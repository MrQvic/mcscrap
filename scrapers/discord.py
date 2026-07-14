import logging
from collections.abc import Mapping
from datetime import datetime

import httpx

from .config import DISCORD_WEBHOOK_URL
from .models import SiteRunResult

logger = logging.getLogger("mc.discord")

_WEBHOOK_TIMEOUT_S = 10.0
_FIELD_VALUE_LIMIT = 1_024
_STATUS_ICONS = {
    "success": "✅",
    "skipped": "⏭️",
    "failed": "❌",
}


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def send_run_summary(
    results: Mapping[str, SiteRunResult],
    started_at: datetime,
    finished_at: datetime,
    next_run_at: datetime,
) -> None:
    """Send one Discord embed summarizing a completed voting run."""
    if not DISCORD_WEBHOOK_URL:
        logger.debug("Discord webhook is not configured; summary not sent")
        return

    failed_count = sum(result.status == "failed" for result in results.values())
    if failed_count:
        title = "MCScrap – běh dokončen s chybami"
        color = 0xED4245
    else:
        title = "MCScrap – běh dokončen"
        color = 0x57F287

    fields = [
        {
            "name": f"{_STATUS_ICONS[result.status]} {site_name}",
            "value": _truncate(result.detail, _FIELD_VALUE_LIMIT),
            "inline": False,
        }
        for site_name, result in results.items()
    ]

    duration_s = max(0.0, (finished_at - started_at).total_seconds())
    payload = {
        "username": "MCScrap",
        "allowed_mentions": {"parse": []},
        "embeds": [
            {
                "title": title,
                "color": color,
                "fields": fields,
                "timestamp": finished_at.astimezone().isoformat(),
                "footer": {
                    "text": (
                        f"Trvání: {duration_s:.1f} s • "
                        f"Další běh: {next_run_at.strftime('%Y-%m-%d %H:%M:%S')}"
                    )
                },
            }
        ],
    }

    try:
        response = httpx.post(
            DISCORD_WEBHOOK_URL,
            json=payload,
            timeout=_WEBHOOK_TIMEOUT_S,
        )
        response.raise_for_status()
        logger.info("Discord run summary sent")
    except httpx.HTTPStatusError as e:
        # Do not include the exception text: it contains the secret webhook URL.
        logger.warning(
            "Discord webhook returned HTTP %d; summary was not sent",
            e.response.status_code,
        )
    except httpx.RequestError as e:
        # Do not include the exception text: it can contain the secret webhook URL.
        logger.warning(
            "Discord webhook request failed (%s); summary was not sent",
            type(e).__name__,
        )
    except Exception as e:
        # Discord reporting must never interrupt the voting loop.
        logger.warning(
            "Unexpected Discord reporting error (%s); summary was not sent",
            type(e).__name__,
        )
