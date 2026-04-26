"""Gmail Internship Monitor — orchestrator.

Cold start
  1. Connect to existing Chrome on CDP port 9222 (or launch fresh).
  2. Wait for the user to log in (manual, only step that needs hands).
  3. Create every label in LABELS that isn't already in the sidebar.
  4. Warm-load the CV-match embedding model + chunk topic table.

Per cycle (default 2 min)
  Step 1 — read_emails.read(page, ...)        scan + recency + full body
  Step 2 — classifier.classify_emails(emails) scam → RAG → labels
  Step 3 — apply_labels.apply(page, labels)   per-thread 'Label as' flow
  Step 4 — append per-email record to history.jsonl
  Step 5 — sleep cycle interval

Each step is wrapped in try/except. On a step failure the agent logs
the error, runs the same primitive locally if a graceful degradation is
possible, and otherwise skips to the next cycle. Partial results from
read_emails are passed through to classify + apply — labelling what we
have rather than dropping the whole cycle.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from playwright.async_api import (
    Browser, BrowserContext, Page, Playwright, async_playwright,
)

import apply_labels
import classifier
import read_emails
from cv_match import get_matcher

sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

# Topic labels + Scam Risk. Edit this list to add or remove categories.
# At startup the agent creates each missing label in Gmail.
LABELS = [
    "AI/ML",
    "Research",
    "Software Engineering",
    "Embedded Systems",
    "DevOps",
    "Scam Risk",
]

CDP_PORT = 9222
CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
HISTORY_FILE = Path("history.jsonl")


# ── Browser bootstrap ───────────────────────────────────────────────────────

def _start_chrome(temp_dir: str) -> subprocess.Popen:
    """Launch real Chrome with a fresh empty profile and CDP open."""
    return subprocess.Popen(
        [
            CHROME_PATH,
            f"--remote-debugging-port={CDP_PORT}",
            f"--user-data-dir={temp_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "https://mail.google.com",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


async def _connect_or_launch_chrome(
    pw: Playwright,
) -> tuple[Browser, BrowserContext, Page, subprocess.Popen | None, str | None]:
    """Reuse a running Chrome on CDP port if present; otherwise launch one
    with a temp profile."""
    try:
        browser = await pw.chromium.connect_over_cdp(
            f"http://localhost:{CDP_PORT}", timeout=2_000,
        )
        print("  Connected to existing Chrome session — skipping login step.")
        chrome_proc, temp_dir = None, None
    except Exception:
        print("  No existing Chrome found — launching a new one.")
        temp_dir = tempfile.mkdtemp(prefix="gmail_monitor_")
        chrome_proc = _start_chrome(temp_dir)
        browser = None
        for _ in range(10):
            try:
                browser = await pw.chromium.connect_over_cdp(
                    f"http://localhost:{CDP_PORT}",
                )
                break
            except Exception:
                await asyncio.sleep(1)
        if browser is None:
            raise RuntimeError(
                f"Could not connect to Chrome on port {CDP_PORT} after launching."
            )

    context = browser.contexts[0] if browser.contexts else await browser.new_context()
    page: Page | None = None
    for ctx in browser.contexts:
        for p in ctx.pages:
            if "mail.google.com" in p.url:
                page = p
                break
        if page:
            break
    if page is None:
        page = context.pages[0] if context.pages else await context.new_page()
    return browser, context, page, chrome_proc, temp_dir


# ── Label setup ─────────────────────────────────────────────────────────────

async def _label_in_sidebar(page: Page, name: str) -> bool:
    for sel in (
        f'a[aria-label="{name}"]',
        f'a[aria-label^="{name} "]',  # Gmail appends e.g. "1 unread has menu"
        f'.aim a[href*="label"]:has-text("{name}")',
        f'div.aio:has-text("{name}")',
    ):
        if await page.locator(sel).count() > 0:
            return True
    return False


async def _create_one_label(page: Page, name: str) -> None:
    """Create a Gmail label via the sidebar '+' button. Idempotent —
    bails out early if the label already exists. Uses input.type() +
    Enter on the MDC default-action button (NOT fill, which leaves the
    Create button disabled because Material Components watches keystroke
    events, not value-set events)."""
    if await _label_in_sidebar(page, name):
        print(f"  Label '{name}' already exists.")
        return

    plus_sel = '[aria-label="Create new label"][data-tooltip="Create new label"]'
    plus = page.locator(plus_sel).first
    try:
        await plus.wait_for(state="visible", timeout=10_000)
    except Exception:
        print(f"  WARNING: '+' button not found (cannot create '{name}').")
        return
    await plus.click()
    await asyncio.sleep(1.5)

    dlg = page.locator('div.uW2Fw-JD').first
    try:
        await dlg.wait_for(state="visible", timeout=5_000)
    except Exception:
        print(f"  WARNING: New-label dialog did not appear for '{name}'.")
        return

    inp = dlg.locator('input[type="text"]').first
    await inp.click()
    await inp.type(name, delay=40)
    await asyncio.sleep(0.4)
    await inp.press("Enter")

    try:
        await dlg.wait_for(state="hidden", timeout=8_000)
        print(f"  Label '{name}' creation submitted.")
    except Exception:
        try:
            create_btn = dlg.locator('button:has-text("Create")').first
            await create_btn.click(timeout=4_000)
            await dlg.wait_for(state="hidden", timeout=6_000)
            print(f"  Label '{name}' creation submitted (via Create click).")
        except Exception:
            print(f"  WARNING: Dialog did not close for '{name}'.")


async def _ensure_labels(page: Page) -> None:
    await page.goto("https://mail.google.com", wait_until="domcontentloaded")
    await page.wait_for_selector('[gh="cm"]', timeout=30_000)
    await asyncio.sleep(3)
    for name in LABELS:
        await _create_one_label(page, name)


async def _verify_labels(page: Page) -> list[str]:
    await asyncio.sleep(1.5)
    missing = [n for n in LABELS if not await _label_in_sidebar(page, n)]
    if not missing:
        return []
    await page.goto(
        "https://mail.google.com/mail/u/0/#settings/labels",
        wait_until="domcontentloaded",
    )
    await asyncio.sleep(3)
    body_text = await page.evaluate("() => document.body.innerText")
    still_missing = [n for n in missing if n not in body_text]
    await page.goto("https://mail.google.com", wait_until="domcontentloaded")
    await page.wait_for_selector('[gh="cm"]', timeout=30_000)
    return still_missing


# ── Cycle steps ─────────────────────────────────────────────────────────────

async def _run_read(
    page: Page, cycle_seconds: int, seen: set[str],
) -> list[dict]:
    """Step 1: read fresh emails the agent hasn't processed yet. On
    exception, log + return partial set so step 2/3 can still run on
    whatever made it through."""
    try:
        return await read_emails.read(
            page, cycle_seconds=cycle_seconds, seen_thread_ids=seen,
        )
    except Exception as e:
        print(f"  [read] step failed: {e}")
        return []


def _load_seen() -> set[str]:
    """Populate the seen-set from history.jsonl so a restart doesn't
    re-process emails the agent has already handled in a prior session."""
    seen: set[str] = set()
    if not HISTORY_FILE.exists():
        return seen
    for line in HISTORY_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            seen.add(json.loads(line)["thread_id"])
        except Exception:
            continue
    return seen


def _run_classify(emails: list[dict]) -> dict[str, list[str]]:
    """Step 2: pure-Python decision step (scam → RAG → topic union)."""
    try:
        return classifier.classify_emails(emails)
    except Exception as e:
        print(f"  [classify] step failed: {e}")
        return {}


async def _run_apply(
    page: Page, thread_labels: dict[str, list[str]],
) -> tuple[int, int]:
    if not thread_labels:
        return 0, 0
    try:
        return await apply_labels.apply(page, thread_labels)
    except Exception as e:
        print(f"  [apply] step failed: {e}")
        return 0, sum(len(v) for v in thread_labels.values())


def _append_history(emails: list[dict], thread_labels: dict[str, list[str]]) -> None:
    cycle_at = time.strftime("%Y-%m-%d %H:%M:%S")
    with HISTORY_FILE.open("a", encoding="utf-8") as f:
        for e in emails:
            record = dict(e)
            record["labels_applied"] = thread_labels.get(e["thread_id"], [])
            record["cycle_at"] = cycle_at
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ── Main ────────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Gmail Internship Monitor")
    p.add_argument(
        "--interval", type=float, default=2.0,
        help="Cycle length in minutes — also the email recency window. Default: 2.",
    )
    return p.parse_args()


async def main() -> None:
    args = _parse_args()
    if args.interval <= 0:
        raise SystemExit("--interval must be positive")
    cycle_seconds = int(args.interval * 60)

    print("=== Gmail Internship Monitor ===")
    print(f"Cycle: {args.interval:g} min\n")

    pw = await async_playwright().start()
    browser: Browser | None = None
    try:
        # ── Cold start ──────────────────────────────────────────────────────
        print("[1/4] Setting up Chrome...")
        browser, context, page, *_ = await _connect_or_launch_chrome(pw)
        if "mail.google.com" not in page.url:
            await page.goto("https://mail.google.com", wait_until="domcontentloaded")
        print("[1/4] Done.\n")

        print("[2/4] Waiting for Gmail login (up to 5 minutes)...")
        await page.wait_for_selector('[gh="cm"]', timeout=300_000)
        print("[2/4] Login confirmed.\n")

        print(f"[3/4] Setting up {len(LABELS)} label(s)...")
        await _ensure_labels(page)
        missing = await _verify_labels(page)
        if missing:
            raise RuntimeError(f"Labels missing after creation: {missing}")
        print(f"  VERIFIED: all {len(LABELS)} labels exist.")
        print("  Loading CV-match embedding model...")
        m = get_matcher()
        print(f"  CV indexed as {len(m.chunks)} chunks.\n")

        # ── Monitoring loop ─────────────────────────────────────────────────
        print(f"[4/4] Starting monitoring loop (every {args.interval:g} min).\n")
        # Seen-set: every thread_id we've already processed. Persisted via
        # history.jsonl so restarts don't re-handle past emails. Updated
        # in-memory at the end of each successful cycle.
        seen = _load_seen()
        if seen:
            print(f"  Loaded {len(seen)} already-processed thread(s) from history.jsonl.\n")
        cycle = 1
        while True:
            print("-" * 50)
            print(f"Cycle {cycle} — {time.strftime('%Y-%m-%d %H:%M:%S')}")

            # Step 1: read (skips thread_ids in `seen`)
            emails = await _run_read(page, cycle_seconds, seen)

            if not emails:
                print("  No new emails this cycle.")
            else:
                print(f"  Read {len(emails)} email(s).")
                # Step 2: classify (mutates each email to add scam_features + cv_match)
                thread_labels = _run_classify(emails)
                # Step 3: apply
                applied, requested = await _run_apply(page, thread_labels)
                for tid, labels in thread_labels.items():
                    subject = next(
                        (e["subject"] for e in emails if e["thread_id"] == tid), tid,
                    )
                    print(f"  [LABELED {','.join(labels)}] {subject[:50]}")
                print(f"  Applied {applied}/{requested} (thread, label) pair(s).")
                # Step 4: history + seen tracking
                _append_history(emails, thread_labels)
                for e in emails:
                    seen.add(e["thread_id"])

            cycle += 1
            print(f"\nSleeping {args.interval:g} min until next cycle...")
            await asyncio.sleep(cycle_seconds)

    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        if browser is not None:
            try:
                await browser.close()
            except Exception:
                pass
        await pw.stop()


if __name__ == "__main__":
    asyncio.run(main())
