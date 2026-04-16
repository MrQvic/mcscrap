from datetime import datetime, timedelta

import json
import logging

import httpx
from bs4 import BeautifulSoup
from playwright.sync_api import BrowserContext

from ..http import AJAX_HEADERS, http_get
from ..models import VoteInfo
from ..config import CAPTCHA_TIMEOUT_MS

logger = logging.getLogger("mc.craftlist")

VOTERS_URL = (
    "https://craftlist.org/{server_slug}/voters"
    "?page=10&month={month}&do=showMore"
)
VOTE_URL = "https://craftlist.org/{server_slug}?nickname={nickname}"

RECAPTCHA_IFRAME = 'iframe[title="reCAPTCHA"]'
RECAPTCHA_CHECKED = '#recaptcha-anchor.recaptcha-checkbox-checked'
VOTE_BUTTON_SELECTOR = 'button[data-lfv-message-id="frm-voteForm-_submit_message"]'

# Cookie consent banner — appears on first visit, then persisted in the Chrome
# profile. We target the stable attributes (class + data-role), not the brittle
# nth-child path that DevTools "Copy selector" produces.
COOKIE_ACCEPT_BUTTON = 'button.cm__btn[data-role="all"]'

# Max time we wait for the captcha to be solved (by Nopecha, or manually in debug).
# Max time we wait for the captcha to be solved — configured via CAPTCHA_TIMEOUT_MS in .env.

# Cooldown between two votes on craftlist.org. Used to compute next_vote_at
# from the timestamp of the player's most recent vote shown in the voter table.
VOTE_DELAY = timedelta(hours=2)


class CraftList:
    """
    Site adapter for craftlist.org.

    Phase A: scrape the current-month voter table. `page=10` forces a single
    paginated request large enough to include all voters for any reasonable
    server (servers rarely exceed one page worth of voters).
    Phase B: TODO — open vote page, solve captcha, submit, verify success.
    """

    def __init__(self, server_slug: str):
        self.server_slug = server_slug

    def get_vote_info(self, nickname: str) -> VoteInfo | None:
        url = VOTERS_URL.format(
            server_slug=self.server_slug,
            month=datetime.now().month,
        )
        # The showMore endpoint requires a valid session cookie — fetch the main
        # voters page first so the server sets one, then request the full list.
        base_url = f"https://craftlist.org/{self.server_slug}/voters"
        with httpx.Client(headers=AJAX_HEADERS, follow_redirects=True, timeout=15) as client:
            client.get(base_url)
            response = client.get(url)
            response.raise_for_status()

        data = json.loads(response.text)
        html = data["snippets"]["snippet--voters"]
        soup = BeautifulSoup(html, "html.parser")

        for row in soup.select("tbody tr"):
            # Nickname is in the alt attribute of the player avatar image.
            avatar = row.select_one('img[src*="minotar.net/helm"]')
            # Vote count lives in the desktop-only cell (md breakpoint).
            vote_cell = row.select_one("td.d-none.d-md-table-cell")
            if not (avatar and vote_cell):
                continue

            vote_text = vote_cell.text.strip()
            #debug print the row
            #print(f"[CraftList] found row: avatar_alt={avatar['alt']!r} vote_text={vote_text!r}")
            if not vote_text.isdigit():
                continue

            if avatar["alt"] != nickname:
                continue

            # Last <td> in the row holds the timestamp of the player's latest
            # vote. Formats observed: "08.04 08:59", "dnes 06:28", "včera 11:54".
            cells = row.find_all("td")
            last_vote_at = _parse_last_vote_time(cells[-1].get_text(strip=True)) if cells else None
            next_vote_at = last_vote_at + VOTE_DELAY if last_vote_at else None

            return VoteInfo(votes=int(vote_text), next_vote_at=next_vote_at)

        # Player not found in the voter table — treat as zero votes, not yet eligible.
        return VoteInfo(votes=0, next_vote_at=None)

    def _assert_on_vote_page(self, page, expected_url: str) -> None:
        """
        Defensive check: verify we actually landed on the vote page.

        Raises RuntimeError immediately (before captcha wait) if either the URL
        is wrong or the vote button is missing — avoids wasting captcha timeout
        when the page is broken or redirected somewhere unexpected.
        """
        if page.url != expected_url and self.server_slug not in page.url:
            raise RuntimeError(
                f"[CraftList] Unexpected redirect: expected URL containing "
                f"'{self.server_slug}', got '{page.url}'"
            )

        try:
            page.wait_for_selector(VOTE_BUTTON_SELECTOR, timeout=3_000)
        except Exception:
            raise RuntimeError(
                f"[CraftList] Vote button not found on page '{page.url}' — "
                "page may be broken or layout changed."
            )

    def _dismiss_cookie_banner(self, page) -> None:
        """
        Click the "Povolit všechny" cookie consent button if the banner is present.

        On first visit the banner overlays the page and can block clicks on the
        vote button or captcha. After acceptance, craftlist persists the choice
        in cookies stored in our .chrome_profile/, so subsequent runs won't show
        the banner at all — the short timeout below makes that fast-path cheap.
        """
        try:
            # Short timeout: on repeat runs the banner is gone and we don't want
            # to block for long. Playwright's click() auto-waits for visible+enabled.
            page.locator(COOKIE_ACCEPT_BUTTON).click(timeout=2_000)
            logger.debug("cookie banner dismissed")
        except Exception:
            # Banner not present (already accepted in a previous run) — fine.
            pass

    def vote(self, context: BrowserContext, nickname: str) -> bool:
        """
        Phase B: cast a vote for `nickname` using the shared browser context.

        Flow:
          1. Open vote page.
          2. Defensive check: assert we're on the correct page with the vote button present.
          3. TODO: fill in nickname input if required.
          4. Tick the GDPR / consent checkbox.
          5. Wait for the reCAPTCHA iframe, then poll until Nopecha (or a human
             in debug mode) marks it as solved.
          6. Click the vote/submit button.
          7. TODO: verify success. No confirmed success signal wired up yet,
             so this returns True unconditionally after the click. Replace
             with a real check (flash message, redirect URL, button state).
        """
        page = context.new_page()
        try:
            url = VOTE_URL.format(server_slug=self.server_slug, nickname=nickname)
            logger.info("navigating to %s", url)
            page.goto(url, wait_until="networkidle")

            self._dismiss_cookie_banner(page)

            logger.debug("asserting we are on the vote page")
            self._assert_on_vote_page(page, url)

            logger.debug("waiting for reCAPTCHA iframe")
            page.wait_for_selector(RECAPTCHA_IFRAME, timeout=7_000)

            logger.debug("waiting for reCAPTCHA to be solved")
            recaptcha_frame = page.frame_locator(RECAPTCHA_IFRAME)
            recaptcha_frame.locator(RECAPTCHA_CHECKED).wait_for(timeout=CAPTCHA_TIMEOUT_MS)

            logger.debug("clicking vote button")
            page.click(VOTE_BUTTON_SELECTOR)

            # TODO: replace with a real success check once we observe craftlist's
            # actual success popup/redirect. Currently returns True unconditionally.
            logger.info("vote submitted (success unverified)")
            return True
        finally:
            page.close()


def _parse_last_vote_time(raw: str) -> datetime | None:
    """
    Parse the craftlist.org "last vote" timestamp into a naive local datetime.

    The site renders timestamps in the server's local timezone (CET/CEST).
    We parse them as naive local datetimes and assume the process runs in the
    same timezone. If that stops being true, this needs to become tz-aware.

    Supported formats:
      - "dnes HH:MM"     -> today at HH:MM
      - "včera HH:MM"    -> yesterday at HH:MM
      - "DD.MM HH:MM"    -> current year, rolled back a year if the result
                            would land more than a day in the future
                            (handles year-boundary edge case in January)
    """
    text = raw.strip().lower()
    if not text:
        return None

    now = datetime.now()

    try:
        if text.startswith("dnes"):
            time_part = text.split(maxsplit=1)[1]
            hh, mm = _split_hhmm(time_part)
            return now.replace(hour=hh, minute=mm, second=0, microsecond=0)

        if text.startswith("včera"):
            time_part = text.split(maxsplit=1)[1]
            hh, mm = _split_hhmm(time_part)
            yesterday = now - timedelta(days=1)
            return yesterday.replace(hour=hh, minute=mm, second=0, microsecond=0)

        # "DD.MM HH:MM" — no year present.
        date_part, time_part = text.split()
        day, month = (int(x) for x in date_part.split("."))
        hh, mm = _split_hhmm(time_part)
        candidate = datetime(now.year, month, day, hh, mm)
        # If we parsed e.g. "28.12" in early January, it's actually last year.
        if candidate > now + timedelta(days=1):
            candidate = candidate.replace(year=now.year - 1)
        return candidate
    except (ValueError, IndexError):
        return None


def _split_hhmm(text: str) -> tuple[int, int]:
    hh, mm = text.split(":")
    return int(hh), int(mm)
