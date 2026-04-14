from dataclasses import dataclass
from datetime import datetime


@dataclass
class VoteInfo:
    """Result of Phase A (cheap lookup) for a single player on a single site."""

    votes: int
    next_vote_at: datetime | None  # absolute time; None = unknown / not provided by site
