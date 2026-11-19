"""Playback provider health tracking.

Clients report a playback failure after it happens, naming the provider that
supplied the source. A rolling count lets a provider with a real outage sort
behind healthy ones instead of every user rediscovering it one failure at a time.
Keys are provider ids (``plex``, ``jellyfin``, or an addon's own sub-keys).
"""

from datetime import timedelta

from loguru import logger
from sqlalchemy.orm import Session

from app.config import settings
from app.models import ProviderFailureEvent
from app.time_utils import utc_now


def record_failure(db: Session, provider_key: str, reason: str | None = None) -> None:
    """Log one reported playback failure for a provider.

    Best-effort: a bookkeeping failure must never surface as an error to the
    client that just reported a real playback failure.
    """
    try:
        db.add(
            ProviderFailureEvent(
                provider_key=provider_key,
                reason=(reason or "")[:64] or None,
            )
        )
        db.commit()
    except Exception as e:
        db.rollback()
        logger.warning(
            f"provider_health failure bookkeeping failed for {provider_key}: {e}"
        )


def get_degraded_providers(db: Session) -> set[str]:
    """Providers with at least the configured failure count within the window."""
    window_start = utc_now() - timedelta(
        seconds=settings.provider_health_window_seconds
    )
    try:
        rows = (
            db.query(ProviderFailureEvent.provider_key)
            .filter(ProviderFailureEvent.occurred_at >= window_start)
            .all()
        )
    except Exception as e:
        logger.warning(f"provider_health degraded-provider query failed: {e}")
        return set()

    counts: dict[str, int] = {}
    for (provider_key,) in rows:
        counts[provider_key] = counts.get(provider_key, 0) + 1
    threshold = settings.provider_health_failure_threshold
    return {key for key, count in counts.items() if count >= threshold}
