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
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from playwright.async_api import (
    Browser, BrowserContext, Page, Playwright, async_playwright,
)

import account
import apply_labels
import classifier
import read_emails
from cv_match import get_matcher
from llm import _claude_cli_path

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
    """Populate the seen-set from the active account's history file so a
    restart doesn't re-process emails handled in a prior session."""
    seen: set[str] = set()
    history_file = account.get_active_history_path()
    if not history_file.exists():
        return seen
    for line in history_file.read_text(encoding="utf-8").splitlines():
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
    history_file = account.get_active_history_path()
    with history_file.open("a", encoding="utf-8") as f:
        for e in emails:
            record = dict(e)
            record["labels_applied"] = thread_labels.get(e["thread_id"], [])
            record["cycle_at"] = cycle_at
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ── Claude-as-orchestrator cycle ────────────────────────────────────────────
#
# Per cycle, we hand control to Claude (via `claude -p`) which:
#   1. Calls the read script (read_emails.py CLI) to scrape fresh emails.
#   2. Reads the resulting emails.json + plan.txt and decides labels per email.
#   3. Calls the apply script (apply_labels.py CLI) to apply those labels.
#
# Python (this module) still owns the cycle loop, the history append, and the
# cold-start (Chrome + login + label setup) — Claude is the brain, not the
# scheduler. Each cycle is one `claude -p` invocation; that's the entire
# "decision" surface.

PROJECT = Path(__file__).parent
_CYCLE_PROMPT_TEMPLATE = """\
You are the cycle worker for a Gmail Internship Monitor. Run ONE cycle now.

Working directory: {project}

Tools available: Bash (with venv\\Scripts\\python on PATH), Read, Write.

Step 1 — scan inbox via the read script:
  Run via Bash: `venv\\Scripts\\python read_emails.py --interval {interval} --out emails.json`
  Then Read emails.json.

  If emails.json contains an empty list `[]`, output exactly `0 read, 0 labeled, 0 pairs` and stop. Do NOT run apply_labels.py.

Step 2 — classify each email using your judgement:
  Read plan.txt — that's the candidate's CV. Use it as grounding for label decisions.

  Allowed labels (use ONLY these, case-sensitive):
    "AI/ML", "Research", "Software Engineering", "Embedded Systems", "DevOps", "Scam Risk"

  Rules:
    - "Scam Risk" is exclusive — if you apply it, do NOT add topic labels to the same email. Apply when the email asks for upfront fees, deposits, dollar amounts tied to deposits/fees, or skips standard hiring steps (no interview, fast-track, instant offer).
    - For real internship emails, return EVERY topic from the allowed list whose subject matter is meaningfully present in the email AND is supported by the CV. Multi-label is fine when the email genuinely spans several topics.
    - Off-topic emails (marketing pitches, unrelated domain, generic newsletters) → empty list `[]`.

Step 3 — apply via the add-label script:
  Build the label map: for every email that got at least one label, map its `thread_id` (exact value from emails.json) to its label list.
  Write to to_label.json with this exact shape:
      {{"thread_labels": {{"<thread_id_1>": ["AI/ML"], "<thread_id_2>": ["Scam Risk"]}}}}
  Skip emails with empty label lists — don't add them to the map.

  Run via Bash: `venv\\Scripts\\python apply_labels.py --in to_label.json`

Final output: print exactly one line:
  `<N> read, <M> labeled, <K> pairs`
  where N = total emails in emails.json, M = number of emails with at least one label, K = sum of labels across all labeled emails.
"""


def _run_claude_cycle(cycle_minutes: float) -> tuple[int, int]:
    """One LLM-orchestrated cycle. Claude invokes read_emails.py, decides
    labels using its own judgement grounded in plan.txt, invokes
    apply_labels.py. Returns (n_read, n_labeled). Python (this caller)
    appends per-account history afterward by reading the JSON files
    Claude wrote."""
    cli = _claude_cli_path()
    if not cli:
        print("  [claude] CLI not found — cycle skipped.")
        return 0, 0

    prompt = _CYCLE_PROMPT_TEMPLATE.format(
        project=str(PROJECT).replace("\\", "/"),
        interval=cycle_minutes,
    )

    # Clean stale to_label.json so we never apply the previous cycle's
    # decisions if Claude bails out before writing a new one.
    label_path = PROJECT / "to_label.json"
    label_path.unlink(missing_ok=True)

    print("  [claude] orchestrating cycle…")
    sys.stdout.flush()
    try:
        result = subprocess.run(
            [str(cli), "-p", prompt,
             "--output-format", "text",
             "--dangerously-skip-permissions"],
            capture_output=True, text=True, encoding="utf-8",
            timeout=600,
            cwd=str(PROJECT),
            env={**os.environ},
        )
    except subprocess.TimeoutExpired:
        print("  [claude] cycle timed out after 10 min.")
        return 0, 0

    if result.returncode != 0:
        print(f"  [claude] cycle failed (exit {result.returncode}):")
        print(f"    stderr: {result.stderr.strip()[:400]}")
        return 0, 0

    out = (result.stdout or "").strip()
    summary = out.splitlines()[-1] if out else "(no output)"
    print(f"  [claude] {summary}")

    # Reconstruct the cycle outcome from the JSON files Claude wrote so
    # we can append authoritative history (don't trust the chat summary).
    emails_path = PROJECT / "emails.json"
    emails: list[dict] = []
    if emails_path.exists():
        try:
            data = json.loads(emails_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                emails = data
        except Exception:
            pass

    thread_labels: dict[str, list[str]] = {}
    if label_path.exists():
        try:
            payload = json.loads(label_path.read_text(encoding="utf-8"))
            tl = payload.get("thread_labels") or {}
            if isinstance(tl, dict):
                thread_labels = tl
        except Exception:
            pass

    if emails:
        for tid, labels in thread_labels.items():
            subject = next(
                (e["subject"] for e in emails if e["thread_id"] == tid), tid,
            )
            print(f"  [LABELED {','.join(labels)}] {subject[:50]}")
        _append_history(emails, thread_labels)

    return len(emails), len(thread_labels)


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
    print(f"Cycle: {args.interval:g} min")
    active_email = account.get_active_email()
    history_file = account.get_active_history_path()
    print(f"Account: {active_email or '(not set — using legacy history.jsonl)'}")
    print(f"History: {history_file.name}\n")

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
        # Cold start is done. Release the in-process Chrome connection so
        # per-cycle CLIs (read_emails.py, apply_labels.py invoked by Claude)
        # can connect to CDP themselves without contention. The Chrome
        # process itself stays alive — only the Playwright client closes.
        try:
            await browser.close()
        except Exception:
            pass
        browser = None

        print(f"[4/4] Starting monitoring loop (every {args.interval:g} min).")
        print("  Each cycle: Claude calls read_emails.py → decides labels → calls apply_labels.py.\n")
        cycle = 1
        while True:
            print("-" * 50)
            print(f"Cycle {cycle} — {time.strftime('%Y-%m-%d %H:%M:%S')}")
            n_read, n_labeled = _run_claude_cycle(args.interval)
            if n_read == 0:
                print("  No new emails this cycle.")
            else:
                print(f"  Cycle complete: {n_read} read, {n_labeled} labeled.")
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
