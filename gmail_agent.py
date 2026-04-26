"""Gmail Internship Monitor — fully automated.

How it works:
  1. Launches the user's real Chrome (no profile) at gmail.com.
  2. Waits for the user to log in manually (the only manual step).
  3. Creates the Gmail label 'Internship Match' if it doesn't exist.
  4. Every cycle (default 2 min, set via --interval):
       a. Scans the inbox and keeps only emails received in that window.
       b. Writes emails.json + prints to stdout.
       c. Waits for Claude Code to drop to_label.json.
       d. Applies the label to threads listed in to_label.json — each
          thread independently (tick row → bulk More → 'Label as' →
          click the menuitemcheckbox → untick).
       e. Sleeps the cycle interval, then repeats.

IPC protocol:
  emails.json   — written by this script; Claude Code reads it to decide.
  to_label.json — written by Claude Code: {"thread_ids": ["abc", "def", ...]}
                  This script consumes (deletes) it after reading.

Why real Chrome instead of Playwright's bundled Chromium:
  Google blocks sign-in on Playwright's Chromium (automation detection).
  Launching real Chrome as a subprocess and connecting via CDP avoids this.
"""

import argparse
import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from playwright.async_api import async_playwright, Browser, BrowserContext, Page, Playwright

sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

LABEL_NAME = "Internship Match"
EMAILS_PER_CYCLE = 50
CDP_PORT = 9222

# Cycle interval is set from --interval at startup (default 10 minutes).
# Both the recency filter and the inter-cycle sleep use this value, so a user
# running the agent at a 2-minute cadence will only consider the last 2 minutes
# of email. The IPC wait is scaled to half the cycle so a tight cadence can't
# block on Claude Code analysis.
CYCLE_INTERVAL_SECONDS = 600
DECISION_TIMEOUT_SECONDS = 300

CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

EMAILS_FILE = Path("emails.json")
TO_LABEL_FILE = Path("to_label.json")


# ── Browser launch ───────────────────────────────────────────────────────────

def _start_chrome(temp_dir: str) -> subprocess.Popen:
    """Launch real Chrome with a fresh empty profile and remote debugging."""
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
    """Connect to an existing Chrome on CDP_PORT if one is running.
    Otherwise launch a new Chrome with a temp profile.

    Returns (browser, context, page, chrome_proc_or_None, temp_dir_or_None).
    chrome_proc/temp_dir are None when we connected to an existing instance.
    """
    # First try connecting to an already-running Chrome (fast path)
    try:
        browser = await pw.chromium.connect_over_cdp(
            f"http://localhost:{CDP_PORT}", timeout=2_000
        )
        print("  Connected to existing Chrome session — skipping login step.")
        chrome_proc, temp_dir = None, None
    except Exception:
        # No existing Chrome — launch a fresh instance
        print("  No existing Chrome found — launching a new one.")
        temp_dir = tempfile.mkdtemp(prefix="gmail_monitor_")
        chrome_proc = _start_chrome(temp_dir)

        # Wait up to 10 seconds for Chrome's CDP port to come online
        browser = None
        for attempt in range(10):
            try:
                browser = await pw.chromium.connect_over_cdp(
                    f"http://localhost:{CDP_PORT}"
                )
                break
            except Exception:
                await asyncio.sleep(1)
        if browser is None:
            raise RuntimeError(
                f"Could not connect to Chrome on port {CDP_PORT} after launching."
            )

    contexts = browser.contexts
    context = contexts[0] if contexts else await browser.new_context()

    # Find an existing Gmail tab if the session was reused; else use first/new page
    page = None
    for ctx in browser.contexts:
        for p in ctx.pages:
            if "mail.google.com" in p.url:
                page = p
                break
        if page:
            break

    if page is None:
        pages = context.pages
        page = pages[0] if pages else await context.new_page()

    return browser, context, page, chrome_proc, temp_dir


# ── Gmail helpers ─────────────────────────────────────────────────────────────

async def _label_in_sidebar(page: Page) -> bool:
    """Quick sidebar check for an existing label entry (not a create button)."""
    for sel in (
        f'a[aria-label="{LABEL_NAME}"]',
        f'.aim a[href*="label"]:has-text("{LABEL_NAME}")',
        f'div.aio:has-text("{LABEL_NAME}")',
    ):
        if await page.locator(sel).count() > 0:
            return True
    return False


async def _create_label(page: Page) -> None:
    """Create the label via the sidebar '+' button next to the 'Labels' heading.

    The previously-attempted Settings → Labels flow is unreliable: the
    "Create new label" link is intercepted by an invisible overlay
    (Gmail's `uW2Fw-JD` panel container). The sidebar '+' button opens the
    same New-label dialog with no overlay interference.
    """
    await page.goto("https://mail.google.com", wait_until="domcontentloaded")
    await page.wait_for_selector('[gh="cm"]', timeout=30_000)
    await asyncio.sleep(3)  # let sidebar finish rendering user labels

    if await _label_in_sidebar(page):
        print(f"  Label '{LABEL_NAME}' already exists.")
        return

    plus_sel = '[aria-label="Create new label"][data-tooltip="Create new label"]'
    plus = page.locator(plus_sel).first
    try:
        await plus.wait_for(state="visible", timeout=10_000)
    except Exception:
        print("  WARNING: '+' button next to 'Labels' not found in sidebar.")
        return
    await plus.click()
    await asyncio.sleep(1.5)

    # Gmail's New-label dialog uses class `uW2Fw-JD`. The actual <input> is
    # a Material-style textbox with no name/aria-label — selecting by type works.
    dlg = page.locator('div.uW2Fw-JD').first
    try:
        await dlg.wait_for(state="visible", timeout=5_000)
    except Exception:
        print("  WARNING: New-label dialog did not appear.")
        return

    # Use type() (not fill) so Material Components' input listener fires and
    # the Create button becomes enabled. Then press Enter — the button has
    # `data-mdc-dialog-button-default`, so Enter submits the dialog reliably
    # even if a programmatic click is intercepted by the MDC backdrop.
    inp = dlg.locator('input[type="text"]').first
    await inp.click()
    await inp.type(LABEL_NAME, delay=40)
    await asyncio.sleep(0.4)
    await inp.press("Enter")

    try:
        await dlg.wait_for(state="hidden", timeout=8_000)
        print(f"  Label '{LABEL_NAME}' creation submitted.")
    except Exception:
        # Fallback: try clicking the Create button now that input has typed text
        try:
            create_btn = dlg.locator('button:has-text("Create")').first
            await create_btn.click(timeout=4_000)
            await dlg.wait_for(state="hidden", timeout=6_000)
            print(f"  Label '{LABEL_NAME}' creation submitted (via Create click).")
        except Exception:
            print("  WARNING: Dialog did not close — creation may have failed.")


async def _verify_label(page: Page) -> bool:
    """Confirm the label exists. Sidebar is checked first (cheap); if not
    visible there, fall back to the labels settings page (authoritative)."""
    await asyncio.sleep(1.5)
    if await _label_in_sidebar(page):
        return True

    # Fallback: navigate to the labels settings page and search the rendered text
    await page.goto(
        "https://mail.google.com/mail/u/0/#settings/labels",
        wait_until="domcontentloaded",
    )
    await asyncio.sleep(3)
    found = await page.evaluate(
        "(name) => document.body.innerText.includes(name)", LABEL_NAME
    )
    await page.goto("https://mail.google.com", wait_until="domcontentloaded")
    await page.wait_for_selector('[gh="cm"]', timeout=30_000)
    return bool(found)


async def _scan_inbox(page: Page) -> list[dict]:
    """Extract subject, sender, snippet, thread ID, and received timestamp from
    inbox rows in one DOM pass.

    Avoids per-email page navigation entirely. The snippet (2-3 sentence preview)
    is sufficient for classifying whether an email is an internship opportunity.
    The timestamp comes from `td.xW span[title]` (Gmail format:
    "Sat, 25 Apr 2026, 16:11"); we normalize the comma so JS Date.parse accepts it.
    """
    await page.goto("https://mail.google.com/#inbox", wait_until="domcontentloaded")
    try:
        await page.wait_for_selector("tr.zA", timeout=15_000)
    except Exception:
        return []

    emails = await page.evaluate(
        """(limit) => {
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

                const snippetEl = row.querySelector('.y2');
                const snippet = snippetEl?.innerText?.trim() || '';

                // Received timestamp from the date column's tooltip.
                // Format: "Sat, 25 Apr 2026, 16:11" — drop the comma before
                // the time so Date.parse treats it as RFC2822-ish.
                const dateEl = row.querySelector('td.xW span[title]');
                const titleStr = dateEl?.getAttribute('title') || '';
                const cleaned = titleStr.replace(/,\\s*(\\d{1,2}:\\d{2})/, ' $1');
                const parsed = cleaned ? Date.parse(cleaned) : NaN;
                const received_ms = Number.isFinite(parsed) ? parsed : null;

                results.push({ thread_id: id, subject, sender, body: snippet, received_ms });
                if (results.length >= limit) break;
            }
            return results;
        }""",
        EMAILS_PER_CYCLE,
    )
    return [e for e in emails if e.get("subject") or e.get("body")]


async def _label_one_thread(page: Page, tid: str) -> bool:
    """Apply LABEL_NAME to a single thread via:
    tick row checkbox → bulk-toolbar More → hover 'Label as' → click the
    `[role="menuitemcheckbox"][title=LABEL_NAME]` entry.

    Each thread is processed in isolation (tick / label / untick) so a failure
    on one thread can't leave residual selection that affects the next one.
    """
    row_index: int = await page.evaluate(
        """(tid) => [...document.querySelectorAll('tr.zA')].findIndex(r =>
            (r.dataset.threadId
              || r.querySelector('[data-thread-id]')?.dataset.threadId
              || r.querySelector('[data-legacy-thread-id]')?.dataset.legacyThreadId) === tid)""",
        tid,
    )
    if row_index < 0:
        print(f"  [WARN] Thread {tid[:24]} not found in inbox view.")
        return False

    def _toggle_row_cb_js() -> str:
        return """(idx) => {
            const row = document.querySelectorAll('tr.zA')[idx];
            if (!row) return false;
            const cb = row.querySelector('.oZ-jc') ||
                       row.querySelector('[role="checkbox"]') ||
                       row.querySelector('td.PF');
            if (cb) { cb.click(); return true; }
            return false;
        }"""

    if not await page.evaluate(_toggle_row_cb_js(), row_index):
        return False
    await asyncio.sleep(1.5)  # bulk toolbar animates in

    try:
        await page.locator('[data-tooltip="More"]').first.click(timeout=8_000)
        await asyncio.sleep(0.8)
        await page.locator(
            '[role="menuitem"]:has-text("Label as")'
        ).first.hover(timeout=4_000)
        await asyncio.sleep(1.5)  # submenu animates open
        # `force=True`: the submenu is animating, so Playwright's stability
        # check times out — but the actual click still reaches Gmail.
        await page.locator(
            f'[role="menuitemcheckbox"][title="{LABEL_NAME}"]'
        ).first.click(timeout=8_000, force=True)
        await asyncio.sleep(0.6)
        return True
    except Exception as e:
        print(f"  [WARN] Could not label {tid[:24]}: {e}")
        return False
    finally:
        # Always close any open menu and untick the row
        try:
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.2)
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.4)
            await page.evaluate(_toggle_row_cb_js(), row_index)
            await asyncio.sleep(0.3)
        except Exception:
            pass


async def _apply_labels_from_inbox(page: Page, thread_ids: list[str]) -> int:
    """Apply LABEL_NAME to each thread in `thread_ids`. Returns the count
    successfully labeled.

    Each thread is labeled independently to avoid the previous bulk-flow bug
    where a single Label-picker action somehow affected unrelated rows.
    """
    if not thread_ids:
        return 0

    await page.goto("https://mail.google.com/#inbox", wait_until="domcontentloaded")
    try:
        await page.wait_for_selector("tr.zA", timeout=15_000)
    except Exception:
        return 0

    labeled = 0
    for tid in thread_ids:
        if await _label_one_thread(page, tid):
            labeled += 1
    return labeled


# ── IPC ──────────────────────────────────────────────────────────────────────

async def _wait_for_decisions() -> list[str]:
    if DECISION_TIMEOUT_SECONDS >= 60:
        wait_str = f"{DECISION_TIMEOUT_SECONDS // 60} min"
    else:
        wait_str = f"{DECISION_TIMEOUT_SECONDS}s"
    print(f"\n  Waiting up to {wait_str} for label decisions (to_label.json)...")
    deadline = time.time() + DECISION_TIMEOUT_SECONDS
    while time.time() < deadline:
        if TO_LABEL_FILE.exists():
            try:
                data = json.loads(TO_LABEL_FILE.read_text(encoding="utf-8"))
                TO_LABEL_FILE.unlink()
                ids = data.get("thread_ids", [])
                print(f"  Received {len(ids)} thread(s) to label.")
                return ids
            except Exception:
                pass
        await asyncio.sleep(2)
    print("  Timed out — skipping labeling this cycle.")
    return []


# ── Main ─────────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Gmail Internship Monitor")
    parser.add_argument(
        "--interval",
        type=float,
        default=2.0,
        help="Cycle length in minutes — also the email-recency window. Default: 2.",
    )
    return parser.parse_args()


async def main() -> None:
    global CYCLE_INTERVAL_SECONDS, DECISION_TIMEOUT_SECONDS
    args = _parse_args()
    if args.interval <= 0:
        raise SystemExit("--interval must be positive")
    CYCLE_INTERVAL_SECONDS = int(args.interval * 60)
    DECISION_TIMEOUT_SECONDS = max(30, CYCLE_INTERVAL_SECONDS // 2)

    print("=== Gmail Internship Monitor ===")
    print(f"Cycle: {args.interval:g} min  ·  IPC wait: {DECISION_TIMEOUT_SECONDS}s\n")

    pw = await async_playwright().start()

    try:
        # ── Step 1: connect to (or launch) Chrome ────────────────────────────
        print("[1/4] Setting up Chrome...")
        browser, context, page, chrome_proc, temp_dir = await _connect_or_launch_chrome(pw)

        # If we connected to existing Chrome, page might already be on Gmail
        if "mail.google.com" not in page.url:
            await page.goto("https://mail.google.com", wait_until="domcontentloaded")
        print("[1/4] Done.\n")

        # ── Step 2: wait for login ───────────────────────────────────────────
        print("[2/4] Waiting for Gmail login (up to 5 minutes)...")
        await page.wait_for_selector('[gh="cm"]', timeout=300_000)
        print("[2/4] Login confirmed.\n")

        # ── Step 3: create + verify label ────────────────────────────────────
        print("[3/4] Setting up label...")
        await _create_label(page)
        if await _verify_label(page):
            print(f"  VERIFIED: '{LABEL_NAME}' exists in Gmail.")
        else:
            print(f"  ERROR: '{LABEL_NAME}' not found after creation attempt.")
            raise RuntimeError(f"Label '{LABEL_NAME}' could not be confirmed in Gmail.")
        print()

        # ── Step 4: monitoring loop ──────────────────────────────────────────
        mins = CYCLE_INTERVAL_SECONDS / 60
        print(f"[4/4] Starting monitoring loop (every {mins:g} min).\n")
        cycle = 1

        while True:
            print("-" * 50)
            print(f"Cycle {cycle} — {time.strftime('%Y-%m-%d %H:%M:%S')}")

            all_emails = await _scan_inbox(page)

            # Keep only emails received within the last cycle window.
            now_ms = time.time() * 1_000
            cutoff_ms = now_ms - CYCLE_INTERVAL_SECONDS * 1_000
            new_emails = [
                e for e in all_emails
                if isinstance(e.get("received_ms"), (int, float))
                and e["received_ms"] >= cutoff_ms
            ][:EMAILS_PER_CYCLE]

            if not new_emails:
                print("No new emails this cycle.")
            else:
                print(f"Found {len(new_emails)} new email(s).")
                for i, e in enumerate(new_emails, 1):
                    print(f"  [{i}] {e['subject'][:70]}")

                EMAILS_FILE.write_text(
                    json.dumps(new_emails, indent=2, ensure_ascii=False), encoding="utf-8"
                )
                print(f"\nemails.json written ({len(new_emails)} emails).")
                print("CLAUDE: please read emails.json, analyze, and write to_label.json.")

                to_label_ids = await _wait_for_decisions()

                labeled_count = await _apply_labels_from_inbox(page, to_label_ids)
                if labeled_count > 0:
                    for tid in to_label_ids:
                        subject = next(
                            (e["subject"] for e in new_emails if e["thread_id"] == tid), tid
                        )
                        print(f"  [LABELED] {subject[:65]}")

                print(f"\nLabeled {labeled_count}/{len(to_label_ids)} emails.")

            cycle += 1
            print(f"\nSleeping {CYCLE_INTERVAL_SECONDS / 60:g} min until next cycle...")
            await asyncio.sleep(CYCLE_INTERVAL_SECONDS)

    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        # Keep Chrome running between sessions — only detach Playwright.
        # The user can manually close Chrome when fully done with the project.
        try:
            await browser.close()  # closes the WS connection only, not Chrome
        except Exception:
            pass
        await pw.stop()
        # Intentionally do NOT terminate chrome_proc or remove temp_dir —
        # this keeps the user's login session alive for the next run.


if __name__ == "__main__":
    asyncio.run(main())
