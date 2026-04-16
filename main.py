import os
import time
from datetime import datetime, timedelta

from dotenv import load_dotenv
from patchright.sync_api import sync_playwright

from scrapers.browser import BrowserManager
from scrapers.models import VoteInfo
from scrapers.sites import CraftList, CzechCraft, MinecraftList, MinecraftServery
from scrapers.state import load_state, save_last_vote

load_dotenv()

NICK = os.getenv("NICK")

# Cooldown used only as a fallback when a site doesn't expose `next_vote_at`.
# Currently applies to MinecraftServery, which has no API/table to parse.
DEFAULT_COOLDOWN = timedelta(hours=2)

# Safety margin added to the earliest next-vote time before we wake up,
# so we don't race the server by a couple of seconds.
WAKEUP_SAFETY_MARGIN = timedelta(seconds=15)


def main() -> dict[str, datetime | None]:
    """
    Run one pass of info-gathering + voting across all sites.

    Returns a mapping of site_name -> effective next_vote_at, where effective means:
      - the site-reported next_vote_at if available, otherwise
      - last_vote_at + DEFAULT_COOLDOWN if we have a stored last vote, otherwise
      - None (never voted, or site doesn't know — the caller should treat None
        as "ready now" and ignore it when computing sleep time).

    The caller uses these values to decide how long to sleep before the next run.
    """
    sites = [
        MinecraftServery(server_slug="goldskyblock-1171"),
        CzechCraft(server_slug="goldskyblock"),
        MinecraftList(server_slug="goldskyblock-y5hf"),
        CraftList(server_slug="goldskyblock"),
    ]

    persisted_last_votes = load_state()

    # Phase A: cheap lookup per site, no browser involved.
    infos: dict[str, VoteInfo | None] = {}
    for site in sites:
        name = type(site).__name__
        try:
            infos[name] = site.get_vote_info(NICK)
        except Exception as e:
            print(f"[{name}] get_vote_info failed: {e}")
            infos[name] = None

    # Compute effective next_vote_at up front so we can log it and reuse it later.
    effective: dict[str, datetime | None] = {
        type(site).__name__: _effective_next_vote_at(
            infos.get(type(site).__name__),
            persisted_last_votes.get(type(site).__name__),
        )
        for site in sites
    }

    for name, info in infos.items():
        if info is None:
            print(f"[{name}] player not found / unavailable")
        else:
            print(
                f"[{name}] votes={info.votes} "
                f"next_vote_at={info.next_vote_at} "
                f"effective_next_vote_at={effective[name]}"
            )

    # Phase B: shared browser context across all sites.
    # Sequential calls respect the nopecha basic 2-concurrent-connection limit.
    with sync_playwright() as p:
        with BrowserManager(p) as context:
            for site in sites:
                name = type(site).__name__

                # Don't attempt to vote if the pre-check failed entirely —
                # we'd be flying blind and likely just burn a captcha.
                if infos.get(name) is None:
                    print(f"[{name}] skipping vote (pre-check failed)")
                    continue

                if not _should_vote(effective[name]):
                    print(f"[{name}] skipping vote (next at {effective[name]})")
                    continue

                try:
                    result = site.vote(context, NICK)
                except NotImplementedError:
                    print(f"[{name}] vote() not implemented yet")
                    continue
                except Exception as e:
                    print(f"[{name}] vote failed: {e}")
                    continue

                if result:
                    print(f"[{name}] vote successful.")
                    now = datetime.now()
                    save_last_vote(name, now)
                    # Refresh our effective time: a successful vote means the
                    # next one is at least DEFAULT_COOLDOWN away. The next
                    # get_vote_info() call (next iteration) will overwrite this
                    # with the site-reported value if available.
                    effective[name] = now + DEFAULT_COOLDOWN
                else:
                    print(f"[{name}] vote failed or unconfirmed.")

    return effective


def _effective_next_vote_at(
    info: VoteInfo | None,
    last_vote_at: datetime | None,
) -> datetime | None:
    """
    Merge site-reported and persisted information into a single next-vote estimate.

    Site-reported `next_vote_at` always wins when present — it's authoritative.
    The persisted `last_vote_at + DEFAULT_COOLDOWN` is a fallback for sites
    that don't expose a next-vote time (currently only MinecraftServery).
    """
    if info is not None and info.next_vote_at is not None:
        return info.next_vote_at
    if last_vote_at is not None:
        return last_vote_at + DEFAULT_COOLDOWN
    return None


def _should_vote(next_vote_at: datetime | None) -> bool:
    """
    Decide whether we should attempt to vote on a site right now.

    Votes only if next_vote_at is in the past or unknown (None).
    """
    if next_vote_at is None:
        return True
    return datetime.now() >= next_vote_at


if __name__ == "__main__":
    print("=== Starting main loop ===")

    while True:
        run_started_at = datetime.now()
        print(f"\n=== Run started at {run_started_at:%Y-%m-%d %H:%M:%S} ===")

        # Default fallback if main() crashes or returns nothing useful.
        effective_times: dict[str, datetime | None] = {}
        try:
            effective_times = main()
        except Exception as e:
            # Catch Exception (not BaseException) so Ctrl+C still aborts the loop.
            print(f"!!! Run crashed: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()

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
