import json
from datetime import datetime

import httpx
from playwright.sync_api import BrowserContext

from ..http import http_get
from ..models import VoteInfo

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
CAPTCHA_TIMEOUT_MS = 12_000


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

    def vote(self, context: BrowserContext, nickname: str) -> bool:
        """
        Phase B: cast a vote for `nickname` using the shared browser context.

        Flow:
          1. Open vote page with nickname in query string.
          2. Defensive check: assert we're on the correct page with expected elements present.
          3. Tick the GDPR consent checkbox.
          4. Wait for the reCAPTCHA iframe, then poll until the checkbox gets
             the `recaptcha-checkbox-checked` class — means Nopecha (or a
             human in debug mode) solved it.
          5. Click the vote/submit button.
          6. TODO: verify success. No confirmed success signal wired up yet,
             so this returns True unconditionally after the click. Replace
             with a real check (flash message, redirect URL, button state).
        """
        page = context.new_page()
        try:
            url = VOTE_URL.format(server_slug=self.server_slug, nickname=nickname)
            print(f"[MinecraftList] navigating to {url}")
            page.goto(url, wait_until="networkidle")

            print("[MinecraftList] asserting we are on the vote page")
            self._assert_on_vote_page(page, url)

            print("[MinecraftList] clicking GDPR checkbox")
            page.click(GDPR_CHECKBOX_SELECTOR)

            print("[MinecraftList] waiting for reCAPTCHA iframe")
            page.wait_for_selector(RECAPTCHA_IFRAME, timeout=7_000)

            print("[MinecraftList] waiting for reCAPTCHA to be solved")
            recaptcha_frame = page.frame_locator(RECAPTCHA_IFRAME)
            recaptcha_frame.locator(RECAPTCHA_CHECKED).wait_for(timeout=CAPTCHA_TIMEOUT_MS)

            print("[MinecraftList] clicking vote button")
            page.click(VOTE_BUTTON_SELECTOR)

            print("[MinecraftList] waiting for result alert")
            try:
                alert = page.locator(VOTE_ALERT_SELECTOR).first
                alert.wait_for(timeout=3_000)
                alert_text = alert.text_content() or ""

                if "Tvůj hlas bude zpracován" in alert_text:
                    print("[MinecraftList] vote successful.")
                    return True
                elif "Již si hlasoval" in alert_text:
                    print("[MinecraftList] already voted.")
                    return True
                else:
                    print(f"[MinecraftList] unknown alert text: '{alert_text.strip()}'")
                    return False
            except Exception:
                print("[MinecraftList] no alert appeared after vote click.")
                return False
        finally:
            page.close()
