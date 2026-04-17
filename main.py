import logging
import os
import time
from datetime import datetime, timedelta

from dotenv import load_dotenv
from patchright.sync_api import sync_playwright

from scrapers.browser import BrowserManager
from scrapers.logger import setup_logging
from scrapers.models import VoteInfo
from scrapers.sites import CraftList, CzechCraft, MinecraftList, MinecraftServery

load_dotenv()
setup_logging()
logger = logging.getLogger("mc.main")

NICK = os.getenv("NICK")

# Map scraper class name -> logger name. Keeps mc.main's per-site log lines
# under the same logger name the scraper itself uses, so output for one site
# shares a single name in the formatted log.
SITE_LOGGER_NAMES = {
    "MinecraftServery": "mc.servery",
    "MinecraftList": "mc.list",
    "CraftList": "mc.craftlist",
    "CzechCraft": "mc.czechcraft",
}

# Total attempts per site per run. The retry covers transient captcha-solver
# failures (NopeCHA timeout, missed Turnstile token, etc.). False returns
# are NOT retried — vote() got a definitive answer from the site and another
# attempt would just burn a captcha credit.
MAX_VOTE_ATTEMPTS = 2

# Pause between vote attempts. Short enough not to noticeably delay the run,
# long enough to avoid hammering the site / captcha solver back-to-back.
RETRY_DELAY_S = 3.0

# Fixed sleep between full runs. All four sites use ~2-hour vote cooldowns,
# so this is the natural cadence — nothing to gain from being clever.
SLEEP_BETWEEN_RUNS_S = 2 * 60 * 60


def _site_logger(name: str) -> logging.Logger:
    """Return the logger associated with a scraper class name."""
    return logging.getLogger(SITE_LOGGER_NAMES.get(name, f"mc.{name.lower()}"))


def _should_vote(info: VoteInfo | None) -> bool:
    """
    Decide whether to attempt a vote on a site given its current info.

    Skips when the pre-check failed entirely (info is None) — voting blind
    would just burn a captcha. Skips when next_vote_at is in the future.
    Votes when next_vote_at is unknown (None) or already in the past.
    """
    if info is None:
        return False
    if info.next_vote_at is None:
        return True
    return datetime.now() >= info.next_vote_at


def _vote_with_retry(site, context, nickname: str, site_log: logging.Logger) -> bool:
    """
    Call site.vote() up to MAX_VOTE_ATTEMPTS times, retrying on exceptions.

    Returns the bool from a successful attempt, or False if all attempts
    raised. NotImplementedError is not retried (stub scraper). False returns
    are not retried either (the site responded definitively — we don't want
    to burn another captcha just to get the same answer).
    """
    for attempt in range(1, MAX_VOTE_ATTEMPTS + 1):
        try:
            return bool(site.vote(context, nickname))
        except NotImplementedError:
            site_log.warning("vote() not implemented yet")
            return False
        except Exception as e:
            if attempt < MAX_VOTE_ATTEMPTS:
                site_log.warning(
                    "vote attempt %d/%d failed: %s; retrying in %.0fs",
                    attempt, MAX_VOTE_ATTEMPTS, e, RETRY_DELAY_S,
                )
                time.sleep(RETRY_DELAY_S)
            else:
                site_log.error(
                    "vote attempt %d/%d failed: %s; giving up",
                    attempt, MAX_VOTE_ATTEMPTS, e,
                )
    return False


def main() -> None:
    sites = [
        MinecraftServery(server_slug="goldskyblock-1171"),
        CzechCraft(server_slug="goldskyblock"),
        MinecraftList(server_slug="goldskyblock-y5hf"),
        CraftList(server_slug="goldskyblock"),
    ]

    # Phase A: cheap lookup per site, no browser involved.
    infos: dict[str, VoteInfo | None] = {}
    for site in sites:
        name = type(site).__name__
        try:
            infos[name] = site.get_vote_info(NICK)
        except Exception as e:
            _site_logger(name).error("get_vote_info failed: %s", e)
            infos[name] = None

    for name, info in infos.items():
        site_log = _site_logger(name)
        if info is None:
            site_log.warning("player not found / unavailable")
        else:
            site_log.info("votes=%s next_vote_at=%s", info.votes, info.next_vote_at)

    # Phase B: shared browser context across all sites.
    # Sequential calls respect the NopeCHA basic 2-concurrent-connection limit.
    with sync_playwright() as p:
        with BrowserManager(p) as context:
            for site in sites:
                name = type(site).__name__
                site_log = _site_logger(name)

                if not _should_vote(infos.get(name)):
                    site_log.info("skipping vote")
                    continue

                if _vote_with_retry(site, context, NICK, site_log):
                    site_log.info("vote successful")
                else:
                    site_log.warning("vote failed or unconfirmed")


if __name__ == "__main__":
    logger.info("=== Starting main loop ===")
    try:
        while True:
            run_started_at = datetime.now()
            logger.info(
                "=== Run started at %s ===",
                run_started_at.strftime("%Y-%m-%d %H:%M:%S"),
            )
            try:
                main()
            except Exception:
                # Catch Exception (not BaseException) so KeyboardInterrupt
                # propagates up to the outer try and exits cleanly.
                logger.exception("!!! Run crashed")

            next_run_at = datetime.now() + timedelta(seconds=SLEEP_BETWEEN_RUNS_S)
            logger.info(
                "=== Run finished. Sleeping until %s (%.1f min) ===",
                next_run_at.strftime("%Y-%m-%d %H:%M:%S"),
                SLEEP_BETWEEN_RUNS_S / 60,
            )
            time.sleep(SLEEP_BETWEEN_RUNS_S)
    except KeyboardInterrupt:
        logger.info("=== Interrupted by user, exiting ===")
