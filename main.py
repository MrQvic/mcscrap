import os
import time
from datetime import datetime, timedelta

from dotenv import load_dotenv
from patchright.sync_api import sync_playwright

from scrapers.browser import BrowserManager
from scrapers.models import VoteInfo
from scrapers.sites import CraftList, CzechCraft, MinecraftList, MinecraftServery

load_dotenv()

NICK = os.getenv("NICK") or "MrKvic_"


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
            print(f"[{name}] get_vote_info failed: {e}")
            infos[name] = None

    for name, info in infos.items():
        if info is None:
            print(f"[{name}] player not found / unavailable")
        else:
            print(f"[{name}] votes={info.votes} next_vote_at={info.next_vote_at}")

    # Phase B: shared browser context across all sites.
    # Sequential calls respect the nopecha basic 2-concurrent-connection limit.
    with sync_playwright() as p:
        with BrowserManager(p) as context:
            for site in sites:
                name = type(site).__name__
                if not _should_vote(infos.get(name)):
                    print(f"[{name}] skipping vote")
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
                else:
                    print(f"[{name}] vote failed or unconfirmed.")


def _should_vote(info: VoteInfo | None) -> bool:
    """
    Decide whether we should attempt to vote on a site given its current info.

    Votes only if next_vote_at is in the past or unknown (None).
    """
    if info is None:
        return False
    if info.next_vote_at is None:
        return True
    return datetime.now() >= info.next_vote_at


if __name__ == "__main__":
    SLEEP_SECONDS = 2 * 60 * 60  # 2 hours between runs

    print("=== Starting main loop ===")

    while True:
        run_started_at = datetime.now()
        print(f"\n=== Run started at {run_started_at:%Y-%m-%d %H:%M:%S} ===")
        try:
            main()
        except Exception as e:
            # Catch Exception (not BaseException) so Ctrl+C still aborts the loop.
            print(f"!!! Run crashed: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()

        next_run_at = datetime.now() + timedelta(seconds=SLEEP_SECONDS)
        print(f"=== Run finished. Sleeping until {next_run_at:%Y-%m-%d %H:%M:%S} ===")
        time.sleep(SLEEP_SECONDS)
