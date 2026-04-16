"""
Persistence of next-vote timestamps per site.

For each site we store the earliest datetime at which a fresh vote should
be attempted. Sources of this value:
  - the site's API (when it exposes `next_vote_at`), or
  - a cooldown notification parsed during a rejected vote attempt, or
  - `now + DEFAULT_COOLDOWN` as a post-success estimate for sites whose
    API doesn't report next-vote times (currently only MinecraftServery).

Used as a fallback when the live API call fails or returns no time.

State is kept in a simple JSON file at the project root. Writes are atomic
(tmp file + os.replace) so Ctrl+C mid-write can't corrupt the file.
"""
import json
import logging
import os
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("mc.state")

STATE_FILE = Path("state.json")


def load_state() -> dict[str, datetime]:
    """Load the next-vote timestamps from disk. Returns {} on any failure."""
    if not STATE_FILE.exists():
        return {}

    try:
        raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("failed to load %s: %s; starting with empty state", STATE_FILE, e)
        return {}

    result: dict[str, datetime] = {}
    for name, iso_str in raw.items():
        try:
            result[name] = datetime.fromisoformat(iso_str)
        except (TypeError, ValueError):
            # Skip malformed entries rather than failing the whole load.
            logger.warning("malformed timestamp for %r: %r; skipping", name, iso_str)
    return result


def save_next_vote(site_name: str, when: datetime) -> None:
    """Atomically update the next-vote timestamp for a single site."""
    state = load_state()
    state[site_name] = when

    serialized = {name: dt.isoformat() for name, dt in state.items()}
    tmp_path = STATE_FILE.with_suffix(".json.tmp")

    try:
        tmp_path.write_text(json.dumps(serialized, indent=2), encoding="utf-8")
        os.replace(tmp_path, STATE_FILE)
    except OSError as e:
        logger.error("failed to persist %s: %s", STATE_FILE, e)
        # Clean up tmp file on failure, ignore if it's already gone.
        try:
            tmp_path.unlink()
        except OSError:
            pass
