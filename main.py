import logging
import os
import time
from datetime import datetime, timedelta

from dotenv import load_dotenv
from patchright.sync_api import sync_playwright

from scrapers.browser import BrowserManager
from scrapers.discord import send_run_summary
from scrapers.logger import setup_logging
from scrapers.models import SiteRunResult, VoteInfo
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
# failures (NopeCHA timeout, missed Turnstile token, etc.). Structured results
# are NOT retried — vote() got a definitive answer from the site and another
# attempt would just burn a captcha credit.
MAX_VOTE_ATTEMPTS = 2

# Pause between vote attempts. Short enough not to noticeably delay the run,
# long enough to avoid hammering the site / captcha solver back-to-back.
RETRY_DELAY_S = 3.0

# Fixed sleep between full runs. All four sites use 2-hour vote cooldowns
SLEEP_BETWEEN_RUNS_S = 2 * 60 * 60 + 60

# Defined at module level so both main() and the startup check share the same
# list without having to instantiate scrapers twice.
SITES = [
    MinecraftServery(server_slug="goldskyblock-1171"),
    CzechCraft(server_slug="goldskyblock"),
    MinecraftList(server_slug="goldskyblock-y5hf"),
    CraftList(server_slug="goldskyblock"),
]


def _site_logger(name: str) -> logging.Logger:
    """Return the logger associated with a scraper class name."""
    return logging.getLogger(SITE_LOGGER_NAMES.get(name, f"mc.{name.lower()}"))


def _describe_exception(error: Exception) -> str:
    """Format an exception without losing useful type information."""
    message = str(error).strip()
    return f"{type(error).__name__}: {message}" if message else type(error).__name__


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


def _vote_with_retry(
    site,
    context,
    nickname: str,
    site_log: logging.Logger,
) -> SiteRunResult:
    """
    Call site.vote() up to MAX_VOTE_ATTEMPTS times, retrying on exceptions.

    A structured result is returned immediately because it is a definitive site
    response. NotImplementedError is not retried. If all attempts raise, the
    final exception is included in the run summary.
    """
    for attempt in range(1, MAX_VOTE_ATTEMPTS + 1):
        try:
            return site.vote(context, nickname)
        except NotImplementedError:
            site_log.warning("vote() not implemented yet")
            return SiteRunResult("failed", "Hlasování pro tento web není implementované.")
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
                return SiteRunResult(
                    "failed",
                    (
                        f"Hlasování selhalo i po {MAX_VOTE_ATTEMPTS} pokusech: "
                        f"{_describe_exception(e)}"
                    ),
                )

    return SiteRunResult("failed", "Hlasování skončilo bez výsledku.")


def _sleep_until(target: datetime, chunk_s: float = 60.0) -> None:
    """
    Sleep until wall-clock datetime.now() reaches target.

    Uses a chunked loop because time.sleep() on Linux uses CLOCK_MONOTONIC,
    while datetime.now() reads wall clock. On WSL2 the two can drift apart
    when the host suspends or resyncs the VM clock, so a single big sleep
    can end at the wrong wall-clock time. Re-checking remaining wall-clock
    time after each chunk makes the sleep track real time.

    Logs WARNING when wall-clock elapsed during a chunk differs from the
    requested sleep duration by more than DRIFT_THRESHOLD_S — evidence of
    a clock jump or resync.
    """
    # Small jitter is normal; multi-second deviation means the wall clock
    # actually jumped (forward or backward) during the sleep.
    DRIFT_THRESHOLD_S = 5.0

    while True:
        remaining = (target - datetime.now()).total_seconds()
        if remaining <= 0:
            return

        sleep_s = min(remaining, chunk_s)
        before = datetime.now()
        time.sleep(sleep_s)
        elapsed = (datetime.now() - before).total_seconds()

        drift = elapsed - sleep_s
        if abs(drift) >= DRIFT_THRESHOLD_S:
            logger.warning(
                "wall clock drift: slept %.1fs, wall clock advanced %.1fs (drift %+.1fs)",
                sleep_s, elapsed, drift,
            )


def main(run_results: dict[str, SiteRunResult]) -> None:
    # Phase A: cheap lookup per site, no browser involved.
    infos: dict[str, VoteInfo | None] = {}
    for site in SITES:
        name = type(site).__name__
        try:
            infos[name] = site.get_vote_info(NICK)
        except Exception as e:
            reason = _describe_exception(e)
            _site_logger(name).error("get_vote_info failed: %s", e)
            infos[name] = None
            run_results[name] = SiteRunResult(
                "failed",
                f"Kontrola dostupnosti hlasování selhala: {reason}",
            )

    for name, info in infos.items():
        site_log = _site_logger(name)
        if info is None:
            site_log.warning("player not found / unavailable")
            run_results.setdefault(
                name,
                SiteRunResult("failed", "Hráč nebyl nalezen nebo je web nedostupný."),
            )
        else:
            site_log.info("votes=%s next_vote_at=%s", info.votes, info.next_vote_at)
            if info.next_vote_at is not None and datetime.now() < info.next_vote_at:
                run_results[name] = SiteRunResult(
                    "skipped",
                    f"Cooldown do {info.next_vote_at.strftime('%Y-%m-%d %H:%M:%S')}.",
                )

    # Phase B: shared browser context across sites that are ready to vote.
    sites_to_vote = []
    for site in SITES:
        name = type(site).__name__
        if name in run_results or not _should_vote(infos.get(name)):
            _site_logger(name).info("skipping vote")
        else:
            sites_to_vote.append(site)

    if not sites_to_vote:
        return

    # Sequential calls respect the NopeCHA basic 2-concurrent-connection limit.
    with sync_playwright() as p:
        with BrowserManager(p) as context:
            for site in sites_to_vote:
                name = type(site).__name__
                site_log = _site_logger(name)
                result = _vote_with_retry(site, context, NICK, site_log)
                run_results[name] = result
                if result.status == "success":
                    site_log.info("vote successful")
                elif result.status == "skipped":
                    site_log.info("vote skipped: %s", result.detail)
                else:
                    site_log.warning("vote failed: %s", result.detail)


def _startup_sleep_if_needed() -> None:
    """
    One-shot check on startup: if no site is ready to vote right now, sleep
    until the latest known next_vote_at instead of wasting a full 2-hour
    cycle. Runs once before the main loop and never again.
    """
    logger.info("=== Startup check ===")
    infos: dict[str, VoteInfo | None] = {}
    for site in SITES:
        name = type(site).__name__
        try:
            infos[name] = site.get_vote_info(NICK)
        except Exception as e:
            _site_logger(name).error("get_vote_info failed: %s", e)
            infos[name] = None

    # Stricter than _should_vote: next_vote_at=None means "unknown", not "ready".
    # MinecraftServery can never determine next_vote_at, so we don't let it
    # short-circuit the startup sleep for the other three sites.
    def _is_ready_for_startup(info: VoteInfo | None) -> bool:
        if info is None or info.next_vote_at is None:
            return False
        return datetime.now() >= info.next_vote_at

    if any(_is_ready_for_startup(info) for info in infos.values()):
        # At least one site with a known cooldown is ready — start immediately.
        logger.info("=== Startup check: at least one site ready, starting immediately ===")
        return

    known_times = [
        info.next_vote_at
        for info in infos.values()
        if info is not None and info.next_vote_at is not None
    ]
    if known_times:
        wake_at = max(known_times)
        sleep_s = max(0.0, (wake_at - datetime.now()).total_seconds())
        logger.info(
            "=== Startup check: no site ready, sleeping until %s (%.1f min) ===",
            wake_at.strftime("%Y-%m-%d %H:%M:%S"),
            sleep_s / 60,
        )
        _sleep_until(wake_at)
    else:
        logger.info(
            "=== Startup check: all sites unavailable, sleeping %.0f min ===",
            SLEEP_BETWEEN_RUNS_S / 60,
        )
        time.sleep(SLEEP_BETWEEN_RUNS_S)


if __name__ == "__main__":
    logger.info("=== Starting main loop ===")
    _startup_sleep_if_needed()
    try:
        while True:
            run_started_at = datetime.now()
            run_results: dict[str, SiteRunResult] = {}
            logger.info(
                "=== Run started at %s ===",
                run_started_at.strftime("%Y-%m-%d %H:%M:%S"),
            )

            crash_reason: str | None = None
            try:
                main(run_results)
            except Exception as e:
                # Catch Exception (not BaseException) so KeyboardInterrupt
                # propagates up to the outer try and exits cleanly.
                crash_reason = _describe_exception(e)
                logger.exception("!!! Run crashed")

            missing_names = [
                type(site).__name__
                for site in SITES
                if type(site).__name__ not in run_results
            ]
            for name in missing_names:
                run_results[name] = SiteRunResult(
                    "failed",
                    (
                        f"Běh byl přerušen: {crash_reason}"
                        if crash_reason
                        else "Web nevrátil výsledek hlasování."
                    ),
                )

            run_finished_at = datetime.now()
            next_run_at = run_finished_at + timedelta(seconds=SLEEP_BETWEEN_RUNS_S)
            ordered_results = {
                type(site).__name__: run_results[type(site).__name__]
                for site in SITES
            }
            if crash_reason and not missing_names:
                ordered_results["Běh"] = SiteRunResult(
                    "failed",
                    f"Neočekávaná chyba běhu: {crash_reason}",
                )
            send_run_summary(
                ordered_results,
                started_at=run_started_at,
                finished_at=run_finished_at,
                next_run_at=next_run_at,
            )

            logger.info(
                "=== Run finished. Sleeping until %s (%.1f min) ===",
                next_run_at.strftime("%Y-%m-%d %H:%M:%S"),
                SLEEP_BETWEEN_RUNS_S / 60,
            )
            _sleep_until(next_run_at)
    except KeyboardInterrupt:
        logger.info("=== Interrupted by user, exiting ===")
