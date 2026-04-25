"""Playwright-based Gmail browser interactions.

All functions assume the browser is already logged in unless noted.
Gmail's DOM changes occasionally — selectors are ordered from most to
least stable. If a selector breaks, check the comment next to it for
what it's targeting so it can be updated quickly.
"""

import asyncio
import json
from pathlib import Path
from typing import Optional

from playwright.async_api import Browser, Page, Playwright, async_playwright

LABEL_NAME = "Internship Match"
_PROCESSED_IDS_FILE = Path("processed_ids.json")
_MAX_STORED_IDS = 20_000  # prevent unbounded file growth


# ---------------------------------------------------------------------------
# Processed-ID persistence
# ---------------------------------------------------------------------------

def load_processed_ids() -> set[str]:
    if _PROCESSED_IDS_FILE.exists():
        try:
            return set(json.loads(_PROCESSED_IDS_FILE.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, ValueError):
            return set()
    return set()


def save_processed_ids(ids: set[str]) -> None:
    trimmed = list(ids)[-_MAX_STORED_IDS:]
    _PROCESSED_IDS_FILE.write_text(json.dumps(trimmed), encoding="utf-8")


# ---------------------------------------------------------------------------
# Browser lifecycle
# ---------------------------------------------------------------------------

async def launch_browser() -> tuple[Playwright, Browser, Page]:
    """Launch a fresh Chromium window with no user profile attached."""
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(
        headless=False,
        args=[
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
        ],
    )
    # new_context() = clean slate: no cookies, no saved sessions
    context = await browser.new_context()
    page = await context.new_page()
    return pw, browser, page


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

async def navigate_and_wait_for_login(page: Page) -> None:
    """Open Gmail and block until the user has logged in (up to 5 minutes)."""
    await page.goto("https://mail.google.com", wait_until="domcontentloaded")
    print("Please log in to Gmail in the browser window.")
    print("Waiting up to 5 minutes for login...")
    # The compose button [gh="cm"] is only rendered after successful login
    await page.wait_for_selector('[gh="cm"]', timeout=300_000)
    print("Login confirmed — inbox loaded.\n")


# ---------------------------------------------------------------------------
# Label management
# ---------------------------------------------------------------------------

async def create_label_if_needed(page: Page) -> None:
    """Create the 'Internship Match' Gmail label if it doesn't exist yet."""
    await page.goto("https://mail.google.com/mail/u/0/#settings/labels")
    await page.wait_for_load_state("networkidle")

    # Gmail renders each label as a table row containing its name as text
    if await page.locator(f'td:has-text("{LABEL_NAME}")').count() > 0:
        print(f"Label '{LABEL_NAME}' already exists.")
    else:
        # "Create new label" is a link at the bottom of the labels settings table
        await page.click('text=/Create new label/i')
        await page.wait_for_selector('[name="lname"]', timeout=8_000)
        await page.fill('[name="lname"]', LABEL_NAME)
        await page.click('button:has-text("Create")')
        await page.wait_for_load_state("networkidle")
        print(f"Label '{LABEL_NAME}' created.")

    # Return to inbox
    await page.goto("https://mail.google.com")
    await page.wait_for_selector('[gh="cm"]', timeout=30_000)


# ---------------------------------------------------------------------------
# Email discovery
# ---------------------------------------------------------------------------

async def get_thread_ids(page: Page, limit: int = 50) -> list[str]:
    """Return up to `limit` thread IDs visible in the inbox."""
    await page.goto("https://mail.google.com/#inbox")
    await page.wait_for_load_state("networkidle")

    try:
        await page.wait_for_selector("tr.zA", timeout=15_000)
    except Exception:
        return []  # empty inbox

    ids: list[str] = await page.evaluate(
        """(limit) => {
            const rows = document.querySelectorAll('tr.zA');
            const ids = [];
            for (const row of rows) {
                // Gmail stores thread IDs in several data-* attributes depending on version
                const id =
                    row.dataset.threadId ||
                    row.querySelector('[data-thread-id]')?.dataset.threadId ||
                    row.querySelector('[data-legacy-thread-id]')?.dataset.legacyThreadId;
                if (id) ids.push(id);
                if (ids.length >= limit) break;
            }
            return ids;
        }""",
        limit,
    )
    return ids


# ---------------------------------------------------------------------------
# Email content extraction
# ---------------------------------------------------------------------------

async def get_email_content(page: Page, thread_id: str) -> Optional[dict]:
    """Open a thread by ID and return {subject, sender, body}, or None on failure."""
    await page.goto(
        f"https://mail.google.com/mail/u/0/#inbox/{thread_id}",
        wait_until="domcontentloaded",
    )
    try:
        # .a3s is Gmail's stable class for the decoded message body
        await page.wait_for_selector(".a3s", timeout=10_000)
    except Exception:
        return None

    # h2.hP = subject header when a thread is open
    subject = (await page.locator("h2.hP").text_content(timeout=3_000) or "").strip()

    # .gD holds the sender element; its "email" attribute has the raw address
    sender_el = page.locator(".gD").first
    try:
        sender = (
            await sender_el.get_attribute("email")
            or await sender_el.text_content()
            or "Unknown"
        ).strip()
    except Exception:
        sender = "Unknown"

    # Collect all message body parts (threads can have multiple replies)
    body_parts = await page.locator(".a3s").all_text_contents()
    body = "\n".join(body_parts).strip()

    return {"subject": subject, "sender": sender, "body": body}


# ---------------------------------------------------------------------------
# Label application
# ---------------------------------------------------------------------------

async def apply_label(page: Page) -> bool:
    """Apply LABEL_NAME to the currently open email thread. Returns True on success.

    Gmail's label button selector changes between releases. We try four
    strategies in order of reliability.
    """
    clicked = False

    # Strategy 1 — tooltip attribute (most stable across Gmail versions)
    for sel in ('[data-tooltip="Label"]', '[data-tooltip*="Label"]'):
        btn = page.locator(sel).first
        try:
            if await btn.is_visible(timeout=1_500):
                await btn.click()
                clicked = True
                break
        except Exception:
            continue

    # Strategy 2 — aria-label attribute
    if not clicked:
        for sel in ('[aria-label="Label"]', '[aria-label*="Label as"]'):
            btn = page.locator(sel).first
            try:
                if await btn.is_visible(timeout=1_500):
                    await btn.click()
                    clicked = True
                    break
            except Exception:
                continue

    # Strategy 3 — "More" overflow menu → "Label as" menu item
    if not clicked:
        try:
            more = page.locator('[aria-label="More"]').first
            await more.click(timeout=3_000)
            item = page.locator('[role="menuitem"]:has-text("Label as")').first
            if await item.is_visible(timeout=2_000):
                await item.click()
                clicked = True
        except Exception:
            pass

    if not clicked:
        return False

    await asyncio.sleep(0.4)  # let the label picker animate open

    try:
        # Try to filter by typing into the search box inside the picker
        search = page.locator(
            '.J-M-Jz input[type="text"], [placeholder*="earch labels"]'
        ).first
        if await search.is_visible(timeout=1_500):
            await search.fill(LABEL_NAME[:12])
            await asyncio.sleep(0.3)

        # Click our label entry (matched by title or visible text)
        label_opt = page.locator(
            f'[title="{LABEL_NAME}"], .J-M-Jz :text-is("{LABEL_NAME}")'
        ).first
        await label_opt.click(timeout=4_000)

        # Some Gmail versions show an explicit "Apply" button; click it if present
        apply_btn = page.locator('button:has-text("Apply")').first
        if await apply_btn.is_visible(timeout=1_000):
            await apply_btn.click()

        return True
    except Exception:
        return False
