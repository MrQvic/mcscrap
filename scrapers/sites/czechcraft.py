import json
from datetime import datetime

import httpx
from playwright.sync_api import BrowserContext

from ..http import http_get
from ..config import CAPTCHA_TIMEOUT_MS
from ..models import VoteInfo

API_URL = "https://czech-craft.eu/api/server/{server_slug}/player/{nickname}/"
VOTE_URL = "https://czech-craft.eu/server/{server_slug}/vote/?user={nickname}"


GDPR_CHECKBOX_SELECTOR = "#privacy"
RECAPTCHA_IFRAME = 'iframe[title="reCAPTCHA"]'
RECAPTCHA_CHECKED = "#recaptcha-anchor.recaptcha-checkbox-checked"
VOTE_BUTTON_SELECTOR = "body > div.container > div.container-left > div > form > button"

# Max time we wait for the captcha to be solved — configured via CAPTCHA_TIMEOUT_MS in .env.

# Format of the `next_vote` field in the API response. Naive local time in the
# server's timezone (CET/CEST). See module README note on tz assumptions.
NEXT_VOTE_FORMAT = "%Y-%m-%d %H:%M:%S"


class CzechCraft:
    """
    Site adapter for czech-craft.eu.

    Phase A: per-player JSON API, 404 = player not found.
    Phase B: TODO — open vote page, solve captcha, submit, verify success.
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
          2. Defensive check: assert we're on the correct page with expected elements present.
          3. Tick the GDPR / consent checkbox.
          4. Wait for the reCAPTCHA iframe, then poll until Nopecha (or a human
             in debug mode) marks it as solved.
          5. Click the vote/submit button.
          6. TODO: verify success. No confirmed success signal wired up yet,
             so this returns True unconditionally after the click. Replace
             with a real check (flash message, redirect URL, button state).
        """
        page = context.new_page()
        try:
            url = VOTE_URL.format(server_slug=self.server_slug, nickname=nickname)
            print(f"[CzechCraft] navigating to {url}")
            page.goto(url, wait_until="networkidle")

            print("[CzechCraft] asserting we are on the vote page")
            self._assert_on_vote_page(page, url)

            page.click(GDPR_CHECKBOX_SELECTOR)
            page.wait_for_selector(RECAPTCHA_IFRAME, timeout=7_000)

            recaptcha_frame = page.frame_locator(RECAPTCHA_IFRAME)
            recaptcha_frame.locator(RECAPTCHA_CHECKED).wait_for(timeout=CAPTCHA_TIMEOUT_MS)

            page.click(VOTE_BUTTON_SELECTOR)

            # TODO: replace with a real success check.
            return True
        finally:
            page.close()
