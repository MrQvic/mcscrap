import json
import logging
import re
from datetime import datetime, timedelta
from html import unescape

from playwright.sync_api import BrowserContext

from ..http import http_get
from ..config import CAPTCHA_TIMEOUT_MS
from ..models import VoteInfo

logger = logging.getLogger("mc.servery")

VOTERS_URL = "https://minecraftservery.eu/voters/{server_slug}"
VOTE_URL = "https://minecraftservery.eu/server/{server_slug}/vote/{nickname}"

# Selectors for the vote page. Placeholders — fill in after inspecting the page.
VOTE_BUTTON_SELECTOR = "#app > main > div.columns > div:nth-child(2) > div > div.modal-card > footer > div > div > button.button.is-primary.has-text-weight-medium"

# Turnstile uses a hidden input that gets populated with a JWT-like token once
# the challenge is solved (by NopeCHA, or by a human in debug mode).
TURNSTILE_IFRAME = 'iframe[src*="challenges.cloudflare.com"]'
TURNSTILE_RESPONSE_INPUT_NAME = "cf-turnstile-response"

# Max time we wait for Turnstile to be solved — configured via CAPTCHA_TIMEOUT_MS in .env.


class MinecraftServery:
    """
    Site adapter for minecraftservery.eu.

    Phase A: scrape voters JSON embedded in the voters page HTML.
    Phase B: TODO — open vote page, solve captcha, submit, verify success.
    """

    def __init__(self, server_slug: str):
        self.server_slug = server_slug

    def get_vote_info(self, nickname: str) -> VoteInfo | None:
        html = http_get(VOTERS_URL.format(server_slug=self.server_slug))

        # The page embeds a JSON blob like `"voters":[{"nickname":"...","count":N}, ...]`
        # inside an HTML-escaped script tag, so we unescape first and regex it out.
        match = re.search(r'"voters":(\[.*?\])', unescape(html))
        if not match:
            raise ValueError("Could not find voters data in page content.")

        voters: list[dict] = json.loads(match.group(1))
        for player in voters:
            if player["nickname"] == nickname:
                # Site does not expose next-vote time anywhere on the voters page.
                return VoteInfo(votes=player["count"], next_vote_at=None)

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
                f"[MinecraftServery] Unexpected redirect: expected URL containing "
                f"'{self.server_slug}', got '{page.url}'"
            )

        try:
            page.wait_for_selector(VOTE_BUTTON_SELECTOR, timeout=2_000)
        except Exception:
            raise RuntimeError(
                f"[MinecraftServery] Vote button not found on page '{page.url}' — "
                "page may be broken or layout changed."
            )

    def vote(self, context: BrowserContext, nickname: str) -> bool | datetime:
        """
        Phase B: cast a vote for `nickname` using the shared browser context.

        Returns:
          - True     -> vote accepted, caller should use DEFAULT_COOLDOWN
          - datetime -> vote rejected on cooldown; this is the authoritative
                        next-vote time parsed from the site's notification
          - False    -> vote failed (captcha, missing popup, unknown response)

        Flow:
          1. Open the vote page with nickname in the query string.
          2. Defensive check: assert we're on the correct page with the vote button present.
          3. Wait for the Turnstile iframe to be present (widget loaded).
          4. Wait until the hidden `cf-turnstile-response` input holds a
             non-empty token — this is the canonical signal that NopeCHA
             (or a human in debug mode) solved the challenge. CSS cannot
             observe the live `value` property, so we poll via JS.
          5. Click the vote/submit button.
          6. Read the resulting notification popup and dispatch on its text.
        """
        page = context.new_page()
        try:
            url = VOTE_URL.format(server_slug=self.server_slug, nickname=nickname)
            logger.info("navigating to %s", url)
            page.goto(url, wait_until="networkidle")

            logger.debug("asserting we are on the vote page")
            self._assert_on_vote_page(page, url)

            logger.debug("waiting for Turnstile iframe")
            page.wait_for_selector(TURNSTILE_IFRAME, timeout=7_000)

            logger.debug("waiting for Turnstile to be solved")
            page.wait_for_function(
                """(inputName) => {
                    const el = document.querySelector(`input[name="${inputName}"]`);
                    return el && el.value && el.value.length > 20;
                }""",
                arg=TURNSTILE_RESPONSE_INPUT_NAME,
                timeout=CAPTCHA_TIMEOUT_MS,
            )

            logger.debug("clicking vote button")
            page.click(VOTE_BUTTON_SELECTOR)

            logger.debug("waiting for notification popup")
            try:
                page.wait_for_selector("div.notification", timeout=5_000)
                notification_text = page.locator("div.notification").first.text_content() or ""
                logger.debug("notification raw text: %r", notification_text)

                if "Hlasovat můžete až v" in notification_text:
                    cooldown_until = _parse_cooldown_time(notification_text)
                    if cooldown_until is not None:
                        # Don't log here — main.py logs the cooldown outcome
                        # uniformly across all sites based on the return type.
                        return cooldown_until
                    # Parsing failed — log and fall back to "vote rejected" so
                    # main.py won't persist a fake success timestamp. Next run
                    # will retry; if the format permanently changed we'll see
                    # repeated warnings here.
                    logger.warning(
                        "cooldown notification present but time unparseable: %r",
                        notification_text,
                    )
                    return False
                elif "byl úspěšně odeslán" in notification_text:
                    # Don't log success here — main.py logs it uniformly
                    # across all sites based on the boolean return value.
                    return True
                elif "Pole captcha je povinné" in notification_text:
                    logger.error("captcha bypass unsuccessful")
                    return False
                else:
                    logger.warning("unknown popup text: %r", notification_text.strip())
                    return False

            except Exception:
                logger.warning("no notification popup detected after click")
                return False
        finally:
            page.close()


# Matches the time portion of "Hlasovat můžete až v HH:MM". The site only
# emits HH:MM (no seconds, no date), so we anchor on two colon-separated
# digit groups and reconstruct the full datetime in _parse_cooldown_time.
_COOLDOWN_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})")


def _parse_cooldown_time(text: str, now: datetime | None = None) -> datetime | None:
    """
    Extract the next-vote time from a cooldown notification.

    Input format observed: " Hlasovat můžete až v 22:50"
    The site reports only HH:MM in local time (no date, no timezone).

    We combine the parsed time with today's date. If the resulting datetime
    is in the past relative to `now`, we assume the cooldown crosses midnight
    and roll forward by one day. The 1-minute grace window protects against
    clock drift between us and the server (we don't want to roll forward when
    the server says "22:50" and we read it at 22:50:03).

    Returns None if no HH:MM pattern is found.
    """
    match = _COOLDOWN_TIME_RE.search(text)
    if not match:
        return None

    hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour < 24 and 0 <= minute < 60):
        return None

    now = now or datetime.now()
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

    # Roll forward across midnight only when the time is meaningfully in the
    # past — a few seconds of drift between server and us shouldn't push the
    # next vote a full day away.
    if candidate < now - timedelta(minutes=1):
        candidate += timedelta(days=1)

    return candidate