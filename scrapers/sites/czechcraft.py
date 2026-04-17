import json
import logging
from datetime import datetime

import httpx
from playwright.sync_api import BrowserContext

from ..http import http_get
from ..config import CAPTCHA_TIMEOUT_MS
from ..models import VoteInfo

logger = logging.getLogger("mc.czechcraft")

API_URL = "https://czech-craft.eu/api/server/{server_slug}/player/{nickname}/"
VOTE_URL = "https://czech-craft.eu/server/{server_slug}/vote/?user={nickname}"


GDPR_CHECKBOX_SELECTOR = "#privacy"
RECAPTCHA_IFRAME = 'iframe[title="reCAPTCHA"]'
RECAPTCHA_CHECKED = "#recaptcha-anchor.recaptcha-checkbox-checked"
VOTE_BUTTON_SELECTOR = "form button.button"

# Result alert shown after submitting the vote. Catches both variants; we
# dispatch on the modifier class (.alert-success vs .alert-error). Note that
# the pre-vote cooldown notice uses the same .alert.alert-error classes — it
# is checked separately BEFORE submit (see COOLDOWN_NOTICE_SELECTOR below).
VOTE_ALERT_SELECTOR = "div.alert"

# Pre-vote cooldown notice rendered on the vote page when the player still
# can't vote (cooldown not elapsed). Same CSS classes as the post-submit error
# alert, so we disambiguate by text — "Hlasovat pro server můžeš" appears only
# in this pre-vote variant. When present, the form is replaced and there's
# nothing to submit, so we short-circuit before captcha.
COOLDOWN_NOTICE_SELECTOR = 'div.alert.alert-error:has-text("Hlasovat pro server můžeš")'

# Max time we wait for the captcha to be solved — configured via CAPTCHA_TIMEOUT_MS in .env.

# Format of the `next_vote` field in the API response. Naive local time in the
# server's timezone (CET/CEST). See module README note on tz assumptions.
NEXT_VOTE_FORMAT = "%Y-%m-%d %H:%M:%S"


class CzechCraft:
    """
    Site adapter for czech-craft.eu
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

        raw_next = data.get("next_vote")
        next_vote_at = datetime.strptime(raw_next, NEXT_VOTE_FORMAT) if raw_next else None

        return VoteInfo(votes=data["vote_count"], next_vote_at=next_vote_at)

    def _assert_on_vote_page(self, page, expected_url: str) -> None:
        """
        Defensive check: verify we actually landed on the vote page.

        Raises RuntimeError immediately (before captcha wait) if either the URL
        is wrong or the expected page elements are missing — avoids wasting
        captcha timeout when the page is broken or redirected somewhere unexpected.
        """
        if page.url != expected_url and self.server_slug not in page.url:
            raise RuntimeError(
                f"[CzechCraft] Unexpected redirect: expected URL containing "
                f"'{self.server_slug}', got '{page.url}'"
            )

        for selector in (GDPR_CHECKBOX_SELECTOR, VOTE_BUTTON_SELECTOR):
            try:
                page.wait_for_selector(selector, timeout=3_000)
            except Exception:
                raise RuntimeError(
                    f"[CzechCraft] Selector '{selector}' not found on page '{page.url}' — "
                    "page may be broken or layout changed."
                )

    def vote(self, context: BrowserContext, nickname: str) -> bool:
        """
        Phase B: cast a vote for `nickname` using the shared browser context.

        Flow:
          1. Open vote page with nickname in query string.
          2. Short-circuit: if the page shows a pre-vote cooldown notice
             instead of the form, return False without attempting captcha.
          3. Defensive check: assert we're on the correct page with expected elements present.
          4. Tick the GDPR / consent checkbox.
          5. Wait for the reCAPTCHA iframe, then poll until Nopecha (or a human
             in debug mode) marks it as solved.
          6. Click the vote/submit button.
          7. Wait for the result alert and dispatch on its CSS class:
             alert-success -> True, alert-error -> False (cooldown), other -> False.
        """
        page = context.new_page()
        try:
            url = VOTE_URL.format(server_slug=self.server_slug, nickname=nickname)
            logger.info("navigating to %s", url)
            page.goto(url, wait_until="networkidle")

            # Pre-vote cooldown short-circuit: when the player can't vote yet,
            # the form is replaced by a static notice. Without this check,
            # _assert_on_vote_page would raise "layout changed" — misleading,
            # since the page is fine, just not in a votable state.
            if page.locator(COOLDOWN_NOTICE_SELECTOR).count() > 0:
                logger.info("vote on cooldown (pre-vote notice present)")
                return False

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
                alert_classes = alert.get_attribute("class") or ""
                alert_text = (alert.text_content() or "").strip()

                if "alert-success" in alert_classes:
                    # Don't log success here — main.py logs it uniformly
                    # across all sites based on the boolean return value.
                    return True
                elif "alert-error" in alert_classes:
                    # Post-submit cooldown alert (e.g. "Již si hlasoval.").
                    # Not parsed — next run's get_vote_info() decides eligibility.
                    logger.info("vote rejected: %s", alert_text)
                    return False
                else:
                    logger.warning("unknown alert classes=%r text=%r", alert_classes, alert_text)
                    return False
            except Exception:
                logger.warning("no alert appeared after vote click")
                return False
        finally:
            page.close()
