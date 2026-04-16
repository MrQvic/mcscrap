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
from scrapers.state import load_state, save_next_vote

load_dotenv()
setup_logging()
logger = logging.getLogger("mc.main")

NICK = os.getenv("NICK")

# Map scraper class name -> logger name. Keeps mc.main's per-site log lines
# under the same logger name the scraper itself uses, so all output for one
# site shares a single name in the formatted log (no "[SiteName]" prefix
# duplicated in the message).
SITE_LOGGER_NAMES = {
    "MinecraftServery": "mc.servery",
    "MinecraftList": "mc.list",
    "CraftList": "mc.craftlist",
    "CzechCraft": "mc.czechcraft",
}


def _site_logger(name: str) -> logging.Logger:
    """Return the logger associated with a scraper class name."""
    return logging.getLogger(SITE_LOGGER_NAMES.get(name, f"mc.{name.lower()}"))

# Cooldown applied as a post-success estimate when a site doesn't expose
# next_vote_at via its API (currently only MinecraftServery). Replaced on
# the next iteration by either a real API value or a parsed cooldown
# notification — whichever comes first.
DEFAULT_COOLDOWN = timedelta(hours=2)

# Safety margin added to the earliest next-vote time before we wake up,
# so we don't race the server by a couple of seconds.
WAKEUP_SAFETY_MARGIN = timedelta(seconds=15)

# How far into the future we're willing to wait *inside the current run*
# for a site to become eligible. If multiple sites are clustered within
# this window (which is typical — voting cooldowns line up across sites),
# we wait inside the loop instead of finishing the run and spinning up a
# fresh one moments later. Avoids: redundant browser launches, repeated
# get_vote_info() calls, and a too-precise on-the-second voting cadence.
VOTE_GRACE_WINDOW = timedelta(seconds=60)


def main() -> dict[str, datetime | None]:
    """
    Run one pass of info-gathering + voting across all sites.

    Returns a mapping of site_name -> effective next_vote_at, where effective is:
      - the site-reported next_vote_at when the API exposes one, otherwise
      - the persisted next_vote_at from disk (set by a prior successful vote,
        cooldown notification, or post-success estimate), otherwise
      - None (no info — the caller should treat None as "ready now" and
        ignore it when computing sleep time).

    The caller uses these values to decide how long to sleep before the next run.
    """
    sites = [
        MinecraftServery(server_slug="goldskyblock-1171"),
        CzechCraft(server_slug="goldskyblock"),
        MinecraftList(server_slug="goldskyblock-y5hf"),
        CraftList(server_slug="goldskyblock"),
    ]

    persisted_next_votes = load_state()

    # Phase A: cheap lookup per site, no browser involved.
    infos: dict[str, VoteInfo | None] = {}
    for site in sites:
        name = type(site).__name__
        try:
            infos[name] = site.get_vote_info(NICK)
        except Exception as e:
            _site_logger(name).error("get_vote_info failed: %s", e)
            infos[name] = None

    # Compute effective next_vote_at up front so we can log it and reuse it later.
    # API is the source of truth when it knows next_vote_at; otherwise we fall
    # back to the persisted value (set by a prior successful vote, cooldown
    # notification, or post-success estimate). For sites whose API never
    # exposes next_vote_at (MinecraftServery), the disk value is the only
    # source. None means "no info — try voting now".
    effective: dict[str, datetime | None] = {}
    for site in sites:
        name = type(site).__name__
        info = infos.get(name)
        if info is not None and info.next_vote_at is not None:
            effective[name] = info.next_vote_at
        else:
            effective[name] = persisted_next_votes.get(name)

    for name, info in infos.items():
        site_log = _site_logger(name)
        if info is None:
            site_log.warning("player not found / unavailable")
        else:
            site_log.info(
                "votes=%s next_vote_at=%s effective_next_vote_at=%s",
                info.votes, info.next_vote_at, effective[name],
            )

    # Phase B: shared browser context across all sites.
    # Sequential calls respect the nopecha basic 2-concurrent-connection limit.
    with sync_playwright() as p:
        with BrowserManager(p) as context:
            for site in sites:
                name = type(site).__name__
                site_log = _site_logger(name)

                # Don't attempt to vote if the pre-check failed entirely —
                # we'd be flying blind and likely just burn a captcha.
                if infos.get(name) is None:
                    site_log.info("skipping vote (pre-check failed)")
                    continue

                wait_seconds = _wait_seconds_until_vote(effective[name])
                if wait_seconds is None:
                    site_log.info("skipping vote (next at %s)", effective[name])
                    continue
                if wait_seconds > 0:
                    site_log.info(
                        "waiting %.0fs for vote slot (opens at %s)",
                        wait_seconds, effective[name],
                    )
                    time.sleep(wait_seconds)

                try:
                    result = site.vote(context, NICK)
                except NotImplementedError:
                    site_log.warning("vote() not implemented yet")
                    continue
                except Exception as e:
                    site_log.error("vote failed: %s", e)
                    continue

                if isinstance(result, datetime):
                    # Cooldown response — vote was NOT registered, but the site
                    # told us when we can try again. This is the authoritative
                    # next-vote time; persist it and use it for the next run.
                    next_vote_at = result.replace(microsecond=0)
                    site_log.info("vote on cooldown until %s", next_vote_at)
                    save_next_vote(name, next_vote_at)
                    effective[name] = next_vote_at
                elif result is True:
                    # Vote actually registered. Estimate cooldown via
                    # DEFAULT_COOLDOWN; the next get_vote_info() call will
                    # replace this with the site-reported value if available.
                    # Truncate to whole seconds for log consistency.
                    next_vote_at = (datetime.now() + DEFAULT_COOLDOWN).replace(microsecond=0)
                    site_log.info("vote successful")
                    save_next_vote(name, next_vote_at)
                    effective[name] = next_vote_at
                else:
                    site_log.warning("vote failed or unconfirmed")

    return effective


def _wait_seconds_until_vote(next_vote_at: datetime | None) -> float | None:
    """
    Decide whether (and how long) to wait for a site's next vote slot in this run.

    Returns:
      - 0.0  -> vote now (slot is open, or unknown)
      - >0   -> vote after waiting this many seconds (slot opens within grace window)
      - None -> skip; the slot is too far away, leave it for a future run
    """
    if next_vote_at is None:
        return 0.0
    delta = (next_vote_at - datetime.now()).total_seconds()
    if delta <= 0:
        return 0.0
    if delta <= VOTE_GRACE_WINDOW.total_seconds():
        return delta
    return None


if __name__ == "__main__":
    print("=== Starting main loop ===")

    try:
        while True:
            run_started_at = datetime.now()
            print(f"\n=== Run started at {run_started_at:%Y-%m-%d %H:%M:%S} ===")

            # Default fallback if main() crashes or returns nothing useful.
            effective_times: dict[str, datetime | None] = {}
            try:
                effective_times = main()
            except Exception:
                # Catch Exception (not BaseException) so KeyboardInterrupt
                # propagates up to the outer try and exits cleanly.
                logger.exception("!!! Run crashed")

            # Wake up when the *earliest* site becomes eligible to vote — we run
            # per-site, so each iteration only needs to handle whichever server is
            # ready next. None values mean "unknown / ready now" and are excluded
            # from the min(); if that leaves us with nothing, fall back to 2h.
            known_times = [t for t in effective_times.values() if t is not None]
            if known_times:
                next_wake = min(known_times) + WAKEUP_SAFETY_MARGIN
            else:
                next_wake = datetime.now() + DEFAULT_COOLDOWN
                print("=== No known next-vote times; falling back to 2h sleep ===")

            sleep_seconds = max(0.0, (next_wake - datetime.now()).total_seconds())
            print(
                f"=== Run finished. Sleeping until {next_wake:%Y-%m-%d %H:%M:%S} "
                f"({sleep_seconds / 60:.1f} min) ==="
            )
            time.sleep(sleep_seconds)
    except KeyboardInterrupt:
        print("\n=== Interrupted by user, exiting ===")
