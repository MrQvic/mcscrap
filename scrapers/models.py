from dataclasses import dataclass
from datetime import datetime
from typing import Literal


@dataclass
class VoteInfo:
    """Result of Phase A (cheap lookup) for a single player on a single site."""

    votes: int
    next_vote_at: datetime | None  # absolute time; None = unknown / not provided by site


@dataclass(frozen=True)
class SiteRunResult:
    """Outcome of one site's work during a voting run."""

    status: Literal["success", "skipped", "failed"]
    detail: str
