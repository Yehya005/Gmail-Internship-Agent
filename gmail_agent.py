"""Gmail Internship Monitor — fully automated.

How it works:
  1. Launches the user's real Chrome (no profile) at gmail.com.
  2. Waits for the user to log in manually (the only manual step).
  3. Creates each topic-specific Gmail label in LABELS if missing
     (AI/ML, Research, Software Engineering, Embedded Systems, DevOps).
  4. Every cycle (default 2 min, set via --interval):
       a. Scans the inbox and keeps only emails received in that window.
       b. Writes emails.json + prints to stdout.
       c. Waits for Claude Code to drop to_label.json.
       d. Applies each thread's chosen labels — each (thread, label)
          pair independently (tick row → bulk More → 'Label as' →
          click the menuitemcheckbox → untick).
       e. Sleeps the cycle interval, then repeats.

IPC formats:
  emails.json   — list of {thread_id, subject, sender, body, received_ms}
  to_label.json — {"thread_labels": {"<thread_id>": ["AI/ML", ...], ...}}

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

from scam_scorer import score_email_dict
from cv_match import get_matcher, match_dict as cv_match_dict
from classifier import classify_email

sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

# Topic-specific labels. Each email can receive zero, one, or several of
# these — Claude Code decides per-email based on the user's CV/skills.
# 'Scam Risk' is orthogonal to the topic labels — applied when scam_scorer
# heuristics + LLM judgment flag an email as a likely fake/predatory offer.
LABELS = [
    "AI/ML",                # AI, ML, Deep Learning, NLP, TF/PyTorch/Keras
    "Research",             # AI/Neuroscience/BCI/EEG/Bioinformatics/DTI roles
    "Software Engineering", # full-stack, Flask, React, web, Python/JS
    "Embedded Systems",     # C, VHDL, digital design, microcontrollers
    "DevOps",               # Docker, Git, Linux, CI/CD, infra
    "Scam Risk",            # deterministic heuristics + LLM judgment flagged this
]
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
# Append-only log of every cycle's emails + their applied labels. Streamlit
# UI reads this file to render the dashboard.
HISTORY_FILE = Path("history.jsonl")


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

async def _label_in_sidebar(page: Page, name: str) -> bool:
    """Quick sidebar check for an existing label entry."""
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
    bails out early if the label already exists in the sidebar."""
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

    # Use type() (not fill) so Material Components' input listener fires and
    # the Create button becomes enabled. Press Enter — the button is the MDC
    # default-action button, so Enter submits the dialog reliably.
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
    """Create all configured labels (idempotent)."""
    await page.goto("https://mail.google.com", wait_until="domcontentloaded")
    await page.wait_for_selector('[gh="cm"]', timeout=30_000)
    await asyncio.sleep(3)
    for name in LABELS:
        await _create_one_label(page, name)


async def _verify_labels(page: Page) -> list[str]:
    """Return any configured labels still missing after creation attempts."""
    await asyncio.sleep(1.5)
    missing = [n for n in LABELS if not await _label_in_sidebar(page, n)]
    if not missing:
        return []

    # Fallback: settings page is authoritative
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
                // Format usually: "Sat, 25 Apr 2026, 16:11" — drop the
                // comma before the time so Date.parse treats it as
                // RFC2822-ish. Fallbacks: try the visible cell text, and
                // if everything fails, use Date.now() so the email is
                // *kept* (we'd rather double-scan than silently drop a
                // recent email whose tooltip didn't parse).
                const dateEl = row.querySelector('td.xW span[title]');
                const titleStr = dateEl?.getAttribute('title') || '';
                const visibleStr = dateEl?.innerText?.trim() || '';
                let parsed = Date.parse(titleStr.replace(/,\\s*(\\d{1,2}:\\d{2})/, ' $1'));
                if (!Number.isFinite(parsed)) parsed = Date.parse(visibleStr);
                const received_ms = Number.isFinite(parsed) ? parsed : Date.now();

                results.push({ thread_id: id, subject, sender, body: snippet, received_ms });
                if (results.length >= limit) break;
            }
            return results;
        }""",
        EMAILS_PER_CYCLE,
    )
    enriched = []
    for e in emails:
        if not (e.get("subject") or e.get("body")):
            continue
        # Attach deterministic scam-risk features (computed locally, no LLM).
        # Claude Code reads these alongside the body when deciding labels.
        e["scam_features"] = score_email_dict(e)
        enriched.append(e)
    return enriched


async def _open_and_get_full_body(page: Page, tid: str) -> str | None:
    """Open the conversation for `tid` and return the concatenated full body
    of every message in the thread, or None if the row can't be found.

    Scammers commonly bury the scam payload (training fee, payment links)
    several paragraphs in, so the row-snippet alone is too short to score
    reliably. This helper navigates into the conversation, scrapes every
    message frame, and navigates back to the inbox.
    """
    # Find the row + click its subject in a single JS evaluate call. Doing
    # the lookup and click atomically avoids the "Element is not attached
    # to the DOM" race when Gmail's SPA re-renders rows between two
    # separate Playwright calls. We dispatch a real MouseEvent chain
    # rather than `el.click()` because Gmail's row handlers listen for
    # mousedown/mouseup, not click.
    result = await page.evaluate(
        """(tid) => {
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
        }""",
        tid,
    )
    if result != 'clicked':
        print(f"    [body-scrape] click prep failed for {tid[:24]}: {result}")
        return None
    await asyncio.sleep(2.0)  # message bodies render after navigation
    print(f"    [body-scrape] opened, url={page.url[-40:]}")

    body = await page.evaluate(
        """() => {
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
    )
    print(f"    [body-scrape] scraped {len(body) if body else 0} chars")

    await page.goto("https://mail.google.com/#inbox", wait_until="domcontentloaded")
    try:
        await page.wait_for_selector("tr.zA", timeout=15_000)
    except Exception:
        pass

    return body


_TOGGLE_ROW_CB_JS = """(idx) => {
    const row = document.querySelectorAll('tr.zA')[idx];
    if (!row) return false;
    const cb = row.querySelector('.oZ-jc') ||
               row.querySelector('[role="checkbox"]') ||
               row.querySelector('td.PF');
    if (cb) { cb.click(); return true; }
    return false;
}"""


async def _label_one_thread(page: Page, tid: str, label_name: str) -> bool:
    """Apply one label to one thread via:
    tick row checkbox → bulk-toolbar More → hover 'Label as' → click the
    `[role="menuitemcheckbox"][title=label_name]` entry → untick row.
    Returns True iff the click on the menuitemcheckbox completed.
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

    if not await page.evaluate(_TOGGLE_ROW_CB_JS, row_index):
        return False
    await asyncio.sleep(1.5)  # bulk toolbar animates in

    try:
        await page.locator('[data-tooltip="More"]').first.click(timeout=8_000)
        await asyncio.sleep(0.8)
        await page.locator(
            '[role="menuitem"]:has-text("Label as")'
        ).first.hover(timeout=4_000)
        await asyncio.sleep(1.5)  # submenu animates open
        # `force=True`: submenu animation defeats Playwright's stability check,
        # but the click still reaches Gmail.
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


async def _apply_labels_from_inbox(
    page: Page, thread_labels: dict[str, list[str]]
) -> tuple[int, int]:
    """Apply each thread's list of labels. Returns (applied_count,
    requested_count) over all (thread, label) pairs."""
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
            if label_name not in LABELS:
                print(f"  [WARN] Unknown label '{label_name}' — skipping.")
                continue
            if await _label_one_thread(page, tid, label_name):
                applied += 1
    return applied, requested


# ── IPC ──────────────────────────────────────────────────────────────────────

async def _wait_for_decisions() -> tuple[dict[str, list[str]], bool]:
    """Read to_label.json and return ({thread_id: [labels]}, timed_out).

    `timed_out=True` means the file never appeared — main loop should fall
    back to the local classifier. `timed_out=False` with an empty dict
    means the LLM explicitly chose to label nothing (don't override).
    """
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
                thread_labels = data.get("thread_labels", {})
                pair_count = sum(len(v) for v in thread_labels.values())
                print(
                    f"  Received {len(thread_labels)} thread(s) "
                    f"with {pair_count} label(s) total."
                )
                return thread_labels, False
            except Exception:
                pass
        await asyncio.sleep(0.5)
    return {}, True


# ── Main ─────────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Gmail Internship Monitor")
    parser.add_argument(
        "--interval",
        type=float,
        default=2.0,
        help="Cycle length in minutes — also the email-recency window. Default: 2.",
    )
    parser.add_argument(
        "--ipc-wait",
        type=int,
        default=20,
        help=(
            "Seconds to wait for an external to_label.json before the local "
            "classifier kicks in. Bump if you're driving labels with a slow "
            "LLM in the loop. Default: 20."
        ),
    )
    return parser.parse_args()


async def main() -> None:
    global CYCLE_INTERVAL_SECONDS, DECISION_TIMEOUT_SECONDS
    args = _parse_args()
    if args.interval <= 0:
        raise SystemExit("--interval must be positive")
    CYCLE_INTERVAL_SECONDS = int(args.interval * 60)
    # IPC wait — how long to wait for an external decision-maker (Claude
    # Code, an LLM API caller, etc.) to drop to_label.json. If nobody
    # writes the file within this window, the local classifier runs
    # autonomously (the common dashboard-driven case). Tight default so
    # the autonomous flow doesn't pay 90 s of dead time per cycle.
    DECISION_TIMEOUT_SECONDS = args.ipc_wait

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

        # ── Step 3: create + verify all configured labels ────────────────────
        print(f"[3/4] Setting up {len(LABELS)} label(s)...")
        await _ensure_labels(page)
        missing = await _verify_labels(page)
        if missing:
            raise RuntimeError(f"Labels missing after creation: {missing}")
        print(f"  VERIFIED: all {len(LABELS)} labels exist.")

        # Warm up the embedding model + CV chunks so cycle 1 doesn't stall
        # on the first cv_match call (model load is ~1-3s on CPU).
        print("  Loading CV-match embedding model...")
        m = get_matcher()
        print(f"  CV indexed as {len(m.chunks)} chunks.\n")

        # ── Step 4: monitoring loop ──────────────────────────────────────────
        mins = CYCLE_INTERVAL_SECONDS / 60
        print(f"[4/4] Starting monitoring loop (every {mins:g} min).\n")
        cycle = 1

        while True:
            print("-" * 50)
            print(f"Cycle {cycle} — {time.strftime('%Y-%m-%d %H:%M:%S')}")

            all_emails = await _scan_inbox(page)

            # Recency window: cycle length + a 1-minute safe zone. So a
            # 2-min cycle accepts emails from the last 3 min, and a 10-min
            # cycle accepts the last 11 min. The safe zone covers two
            # things at once: (a) Gmail's tooltip has minute precision so
            # an email actually received at 11:20:55 reads as 11:20:00,
            # and (b) edge cases where an email lands between the scan
            # call and the cycle's clock read. Re-scanning the same email
            # in two consecutive cycles is safe — labelling is idempotent.
            now_ms = time.time() * 1_000
            cutoff_ms = now_ms - CYCLE_INTERVAL_SECONDS * 1_000 - 60_000
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

                # Open each fresh email and replace the row-snippet body with
                # the full message text, then re-run the scam scorer. Snippet
                # alone is ~100 chars and lets scammers bury the payload.
                # Also compute the CV-match block (RAG over chunked CV) so
                # Claude Code sees matched-evidence + missing-skills when
                # deciding which topic labels to apply.
                print("  Reading full bodies + scoring CV match...")
                for e in new_emails:
                    full = await _open_and_get_full_body(page, e["thread_id"])
                    if full:
                        e["body"] = full
                        e["scam_features"] = score_email_dict(e)
                    # Use whatever body we have (full or snippet) for the match.
                    e["cv_match"] = cv_match_dict(e.get("body") or "")

                EMAILS_FILE.write_text(
                    json.dumps(new_emails, indent=2, ensure_ascii=False), encoding="utf-8"
                )
                print(f"\nemails.json written ({len(new_emails)} emails).")
                print("CLAUDE: please read emails.json, analyze, and write to_label.json.")

                thread_labels, timed_out = await _wait_for_decisions()
                if timed_out:
                    # No LLM/Claude Code in the loop — fall back to the
                    # local rule-based classifier (scam-first, then topic
                    # keywords gated on cv_match score).
                    thread_labels = {
                        e["thread_id"]: lbls
                        for e in new_emails
                        if (lbls := classify_email(e))
                    }
                    print(
                        f"  No to_label.json — auto-classified "
                        f"{len(thread_labels)} thread(s) locally."
                    )

                applied, requested = await _apply_labels_from_inbox(
                    page, thread_labels
                )
                for tid, labels in thread_labels.items():
                    subject = next(
                        (e["subject"] for e in new_emails if e["thread_id"] == tid), tid
                    )
                    print(f"  [LABELED {','.join(labels)}] {subject[:50]}")

                print(f"\nApplied {applied}/{requested} (thread, label) pair(s).")

                # Append every email this cycle to history.jsonl so the
                # Streamlit UI has a complete record. Includes the labels
                # actually applied (empty list for emails we skipped).
                cycle_at = time.strftime("%Y-%m-%d %H:%M:%S")
                with HISTORY_FILE.open("a", encoding="utf-8") as f:
                    for e in new_emails:
                        record = dict(e)
                        record["labels_applied"] = thread_labels.get(e["thread_id"], [])
                        record["cycle_at"] = cycle_at
                        f.write(json.dumps(record, ensure_ascii=False) + "\n")

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
