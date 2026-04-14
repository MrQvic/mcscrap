"""
Phase A smoke test: hit every site's get_vote_info() and print the result.
No browser, no voting — just verify parsers and API shapes still work.

Run: python test.py
"""

from scrapers.models import VoteInfo
from scrapers.sites import CraftList, CzechCraft, MinecraftList, MinecraftServery

NICK = "Safiron8"

SITES = [
    MinecraftServery(server_slug="goldskyblock-1171"),
    CzechCraft(server_slug="goldskyblock"),
    MinecraftList(server_slug="goldskyblock-y5hf"),
    CraftList(server_slug="goldskyblock"),
]


def main() -> None:
    for site in SITES:
        name = type(site).__name__
        try:
            info: VoteInfo | None = site.get_vote_info(NICK)
        except Exception as e:
            print(f"[{name}] FAIL: {type(e).__name__}: {e}")
            continue

        if info is None:
            print(f"[{name}] player not found")
        else:
            print(f"[{name}] votes={info.votes} next_vote_at={info.next_vote_at}")


if __name__ == "__main__":
    main()
