import json
import logging
import re
from datetime import datetime

import httpx
from playwright.sync_api import BrowserContext

from ..http import http_get
from ..models import VoteInfo
from ..config import CAPTCHA_TIMEOUT_MS

logger = logging.getLogger("mc.list")

API_URL = "https://www.minecraft-list.cz/api/server/{server_slug}/player/{nickname}"
VOTE_URL = "https://www.minecraft-list.cz/server/{server_slug}/vote?name={nickname}"

# Format of the `next_vote_at` field in the API response. Naive local time in
# the server's timezone (CET/CEST).
NEXT_VOTE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Selectors for the vote page. Reused from the previous playwright implementation.
GDPR_CHECKBOX_SELECTOR = "#tosgdpr"
RECAPTCHA_IFRAME = 'iframe[title="reCAPTCHA"]'
RECAPTCHA_CHECKED = "#recaptcha-anchor.recaptcha-checkbox-checked"
VOTE_BUTTON_SELECTOR = (
    "#vote-form > div.d-flex.align-items-center.justify-content-between > button"
)
VOTE_ALERT_SELECTOR = '//*[@id="about"]/div/div[1]/div'

# Max time we wait for the captcha to be solved (by Nopecha, or manually in debug).
# Max time we wait for the captcha to be solved — configured via CAPTCHA_TIMEOUT_MS in .env.


class MinecraftList:
    """
    Site adapter for minecraft-list.cz.

    Phase A: per-player JSON API, 404 = player not found.
    Phase B: real browser flow — GDPR checkbox, wait for reCAPTCHA to be
             solved, click vote button, verify success.
    """

    def __init__(self, server_slug: str):
        self.server_slug = server_slug

    def get_vote_info(self, nickname: str) -> VoteInfo | None:
        url = API_URL.format(server_slug=self.server_slug, nickname=nickname)
        try:
            body = http_get(url)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise

        data = json.loads(body)

        raw_next = data.get("next_vote_at")
        next_vote_at = datetime.strptime(raw_next, NEXT_VOTE_FORMAT) if raw_next else None

        return VoteInfo(votes=data["votes_count"], next_vote_at=next_vote_at)

    def _assert_on_vote_page(self, page, expected_url: str) -> None:
        """
        Defensive check: verify we actually landed on the vote page.

        Raises RuntimeError immediately (before captcha wait) if either the URL
        is wrong or the expected page elements are missing — avoids wasting
        captcha timeout when the page is broken or redirected somewhere unexpected.
        """
        if page.url != expected_url and self.server_slug not in page.url:
            raise RuntimeError(
                f"[MinecraftList] Unexpected redirect: expected URL containing "
                f"'{self.server_slug}', got '{page.url}'"
            )

        for selector in (GDPR_CHECKBOX_SELECTOR, VOTE_BUTTON_SELECTOR):
            try:
                page.wait_for_selector(selector, timeout=3_000)
            except Exception:
                raise RuntimeError(
                    f"[MinecraftList] Selector '{selector}' not found on page '{page.url}' — "
                    "page may be broken or layout changed."
                )

    def vote(self, context: BrowserContext, nickname: str) -> bool | datetime:
        """
        Phase B: cast a vote for `nickname` using the shared browser context.

        Returns:
          - True     -> vote accepted, caller should use DEFAULT_COOLDOWN
          - datetime -> vote rejected on cooldown ("Již si hlasoval"); this
                        is the authoritative next-vote time parsed from the
                        site's alert message
          - False    -> vote failed (captcha, missing alert, unknown response)

        Flow:
          1. Open vote page with nickname in query string.
          2. Defensive check: assert we're on the correct page with expected elements present.
          3. Tick the GDPR consent checkbox.
          4. Wait for the reCAPTCHA iframe, then poll until the checkbox gets
             the `recaptcha-checkbox-checked` class — means Nopecha (or a
             human in debug mode) solved it.
          5. Click the vote/submit button.
          6. Read the resulting alert and dispatch on its text.
        """
        page = context.new_page()
        try:
            url = VOTE_URL.format(server_slug=self.server_slug, nickname=nickname)
            logger.info("navigating to %s", url)
            page.goto(url, wait_until="networkidle")

            logger.debug("asserting we are on the vote page")
            self._assert_on_vote_page(page, url)

            logger.debug("clicking GDPR checkbox")
            page.click(GDPR_CHECKBOX_SELECTOR)

            logger.debug("waiting for reCAPTCHA iframe")
            page.wait_for_selector(RECAPTCHA_IFRAME, timeout=7_000)

            logger.debug("waiting for reCAPTCHA to be solved")
            recaptcha_frame = page.frame_locator(RECAPTCHA_IFRAME)
            recaptcha_frame.locator(RECAPTCHA_CHECKED).wait_for(timeout=CAPTCHA_TIMEOUT_MS)

            logger.debug("clicking vote button")
            page.click(VOTE_BUTTON_SELECTOR)

            logger.debug("waiting for result alert")
            try:
                alert = page.locator(VOTE_ALERT_SELECTOR).first
                alert.wait_for(timeout=3_000)
                alert_text = alert.text_content() or ""

                if "Tvůj hlas bude zpracován" in alert_text:
                    # Don't log success here — main.py logs it uniformly
                    # across all sites based on the boolean return value.
                    return True
                elif "Již si hlasoval" in alert_text:
                    cooldown_until = _parse_cooldown_time(alert_text)
                    if cooldown_until is not None:
                        # Don't log here — main.py logs the cooldown outcome
                        # uniformly across all sites based on the return type.
                        return cooldown_until
                    # Parsing failed — log and fall back to "vote rejected" so
                    # main.py won't persist a fake success timestamp. Next run
                    # will retry; if the format permanently changed we'll see
                    # repeated warnings here.
                    logger.warning(
                        "cooldown alert present but time unparseable: %r",
                        alert_text,
                    )
                    return False
                else:
                    logger.warning("unknown alert text: %r", alert_text.strip())
                    return False
            except Exception:
                logger.warning("no alert appeared after vote click")
                return False
        finally:
            page.close()


# Matches the datetime portion of "Již si hlasoval. Znovu můžeš hlasovat v
# DD.MM.YYYY HH:MM:SS". The site emits a fully qualified local timestamp
# (no timezone), so we anchor on the day.month.year hour:minute:second
# pattern and parse it directly into a datetime.
_COOLDOWN_TIME_RE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})\s+(\d{1,2}):(\d{2}):(\d{2})")


def _parse_cooldown_time(text: str) -> datetime | None:
    """
    Extract the next-vote time from a "Již si hlasoval" alert.

    Input format observed: "Již si hlasoval. Znovu můžeš hlasovat v 17.04.2026 00:56:38"
    The site reports a full local datetime (no timezone).

    Returns None if no DD.MM.YYYY HH:MM:SS pattern is found, or if the
    parsed components don't form a valid datetime (e.g. month=13).
    """
    match = _COOLDOWN_TIME_RE.search(text)
    if not match:
        return None

    day, month, year, hour, minute, second = (int(g) for g in match.groups())
    try:
        return datetime(year, month, day, hour, minute, second)
    except ValueError:
        return None
