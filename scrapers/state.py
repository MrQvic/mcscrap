"""
Persistence of last-vote timestamps per site.

Used primarily as a fallback for sites that don't expose `next_vote_at`
through their public API (e.g. MinecraftServery). The stored timestamp
plus a site-specific cooldown gives us an estimated earliest next vote.

State is kept in a simple JSON file at the project root. Writes are atomic
(tmp file + os.replace) so Ctrl+C mid-write can't corrupt the file.
"""
import json
import os
from datetime import datetime
from pathlib import Path

STATE_FILE = Path("state.json")


def load_state() -> dict[str, datetime]:
    """Load the last-vote timestamps from disk. Returns {} on any failure."""
    if not STATE_FILE.exists():
        return {}

    try:
        raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"[state] failed to load {STATE_FILE}: {e}; starting with empty state")
        return {}

    result: dict[str, datetime] = {}
    for name, iso_str in raw.items():
        try:
            result[name] = datetime.fromisoformat(iso_str)
        except (TypeError, ValueError):
            # Skip malformed entries rather than failing the whole load.
            print(f"[state] malformed timestamp for {name!r}: {iso_str!r}; skipping")
    return result


def save_last_vote(site_name: str, when: datetime) -> None:
    """Atomically update the last-vote timestamp for a single site."""
    state = load_state()
    state[site_name] = when

    serialized = {name: dt.isoformat() for name, dt in state.items()}
    tmp_path = STATE_FILE.with_suffix(".json.tmp")

    try:
        tmp_path.write_text(json.dumps(serialized, indent=2), encoding="utf-8")
        os.replace(tmp_path, STATE_FILE)
    except OSError as e:
        print(f"[state] failed to persist {STATE_FILE}: {e}")
        # Clean up tmp file on failure, ignore if it's already gone.
        try:
            tmp_path.unlink()
        except OSError:
            pass
