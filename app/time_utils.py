"""UTC helpers that preserve Curatio's existing naive database timestamps."""

from datetime import UTC, datetime


def utc_now() -> datetime:
    """Return naive UTC for compatibility with existing ``DateTime`` columns."""
    return datetime.now(UTC).replace(tzinfo=None)


def utc_from_timestamp(value: float) -> datetime:
    """Convert a Unix timestamp to naive UTC without deprecated APIs."""
    return datetime.fromtimestamp(value, UTC).replace(tzinfo=None)
