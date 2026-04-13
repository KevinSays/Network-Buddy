"""
Alert dispatch — sends notifications to a generic webhook and/or ntfy.sh.

Configure via .env:
  ALERT_WEBHOOK_URL   — POST JSON {title, message, source} to this URL
  ALERT_NTFY_TOPIC    — publish to https://ntfy.sh/<topic>
"""

import logging
import re
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

# ntfy.sh topic: alphanumeric, hyphens, underscores only (no path traversal)
_NTFY_TOPIC_RE = re.compile(r"^[\w\-]{1,64}$")


def _safe_webhook_url(url: str) -> str | None:
    """Return *url* only if it uses http/https with a non-empty host, else None."""
    if not url:
        return None
    try:
        p = urlparse(url)
        if p.scheme in ("http", "https") and p.netloc:
            return url
    except Exception:
        pass
    logger.warning("ALERT_WEBHOOK_URL %r is not a valid http/https URL — skipping", url)
    return None


def _safe_ntfy_topic(topic: str) -> str | None:
    """Return *topic* only if it is safe to interpolate into the ntfy.sh URL."""
    if not topic:
        return None
    if _NTFY_TOPIC_RE.match(topic):
        return topic
    logger.warning("ALERT_NTFY_TOPIC %r contains invalid characters — skipping", topic)
    return None


async def send(title: str, message: str, webhook_url: str = "", ntfy_topic: str = "") -> None:
    """Fire-and-forget alert.  Silently skips if no destinations are configured."""
    safe_url   = _safe_webhook_url(webhook_url)
    safe_topic = _safe_ntfy_topic(ntfy_topic)
    if not safe_url and not safe_topic:
        return
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            if safe_url:
                await client.post(safe_url, json={
                    "title": title,
                    "message": message,
                    "source": "MHS",
                })
            if safe_topic:
                await client.post(
                    f"https://ntfy.sh/{safe_topic}",
                    content=message,
                    headers={
                        "Title": title,
                        "Priority": "default",
                        "Tags": "electric_plug",
                    },
                )
    except Exception:
        # Log category only — not the exception message — to avoid leaking URLs
        logger.warning("Alert send failed (check network/webhook config)")
