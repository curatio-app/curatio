"""Watch history from the connected Plex/Jellyfin account.

Each library provider signs in as one server account and reports that
account's watch state during sync (``MediaAvailability.play_count`` /
``watched_at``). Profiles that opt in (``User.server_history``) get one
``WatchEvent(action="server_watched")`` per watched title, so a new profile's
recommendations are personal from the first sync instead of after several
plays inside Curatio.

``server_watched`` events shape taste and can seed "Because You Watched" rows,
but they never appear in Continue Watching, Up Next or history (those read
``complete`` events only) and are never sent to scrobble targets.
"""

from __future__ import annotations

from loguru import logger
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Account, MediaAvailability, User, WatchEvent
from app.time_utils import utc_now

SERVER_WATCHED = "server_watched"

#: Event kinds that shape a profile's taste.
TASTE_ACTIONS = ("complete", "imported", SERVER_WATCHED)

#: Event kinds that may seed "Because You Watched": a deliberate watch, either
#: in Curatio or on the profile's own server. Pasted imports may not.
SEED_ACTIONS = ("complete", SERVER_WATCHED)

HISTORY_SETTINGS = ("on", "off")


def normalize_setting(value: object) -> str | None:
    """ "on", "off", or None (automatic)."""
    text = str(value or "").strip().lower()
    return text if text in HISTORY_SETTINGS else None


def _owner_profile_ids(db: Session) -> set[int]:
    """The first profile of each admin account: the server owner's own."""
    owners: set[int] = set()
    for account in db.query(Account).filter(Account.is_admin.is_(True)):
        first = (
            db.query(User.id)
            .filter(User.account_id == account.id, User.is_active.is_(True))
            .order_by(User.created_at, User.id)
            .first()
        )
        if first:
            owners.add(first[0])
    return owners


def uses_server_history(db: Session, user: User) -> bool:
    setting = normalize_setting(getattr(user, "server_history", None))
    if setting is not None:
        return setting == "on"
    return user.id in _owner_profile_ids(db)


def history_profiles(db: Session) -> list[User]:
    """Active profiles whose recommendations use server watch history."""
    owners = _owner_profile_ids(db)
    profiles = []
    for user in db.query(User).filter(User.is_active.is_(True)):
        setting = normalize_setting(user.server_history)
        if setting == "on" or (setting is None and user.id in owners):
            profiles.append(user)
    return profiles


def watched_titles(db: Session) -> dict[tuple[int, str], object]:
    """{(tmdb_id, "movie"|"tv"): last watched} across connected servers."""
    from app.sources.library_base import counts_as_watched

    titles: dict[tuple[int, str], object] = {}
    rows = db.query(
        MediaAvailability.tmdb_id,
        MediaAvailability.media_type,
        MediaAvailability.play_count,
        MediaAvailability.watched_at,
    ).filter(
        MediaAvailability.tmdb_id.isnot(None),
        MediaAvailability.play_count > 0,
    )
    for tmdb_id, media_type, play_count, watched_at in rows:
        if not counts_as_watched(media_type, play_count):
            continue
        key = (int(tmdb_id), "movie" if media_type == "movie" else "tv")
        previous = titles.get(key)
        if previous is None or (watched_at and watched_at > previous):  # type: ignore[operator]
            titles[key] = watched_at or previous
    return titles


def import_for(db: Session, user: User, watched: dict | None = None) -> int:
    """Add missing ``server_watched`` events for ``user``. Returns the count."""
    watched = watched_titles(db) if watched is None else watched
    if not watched:
        return 0
    existing = {
        (int(tmdb_id), media_type)
        for tmdb_id, media_type in db.query(
            WatchEvent.tmdb_id, WatchEvent.media_type
        ).filter(
            WatchEvent.user_id == user.id,
            WatchEvent.action == SERVER_WATCHED,
            WatchEvent.tmdb_id.isnot(None),
        )
    }
    added = 0
    now = utc_now()
    for (tmdb_id, media_type), watched_at in watched.items():
        if (tmdb_id, media_type) in existing:
            continue
        db.add(
            WatchEvent(
                user_id=user.id,
                tmdb_id=tmdb_id,
                media_type=media_type,
                action=SERVER_WATCHED,
                created_at=watched_at or now,
            )
        )
        added += 1
    return added


def import_all() -> dict[int, int]:
    """Import server history for every opted-in profile; rebuild changed ones."""
    changed: dict[int, int] = {}
    with get_db() as db:
        watched = watched_titles(db)
        if not watched:
            return changed
        for user in history_profiles(db):
            added = import_for(db, user, watched)
            if added:
                changed[user.id] = added
        if changed:
            from app.home_feed import invalidate_discovery

            for user_id in changed:
                invalidate_discovery(db, user_id)
        db.commit()
    if changed:
        logger.info("Server history imported: {}", changed)
        _schedule_rebuilds(changed)
    return changed


def _schedule_rebuilds(changed: dict[int, int]) -> None:
    try:
        from app import main

        for user_id in changed:
            main._invalidate_dashboard_cache(user_id)
            main._schedule_local_sync(user_id)
    except Exception as exc:  # the next scheduled sync picks it up anyway
        logger.debug("Server history rebuild scheduling skipped: {}", exc)


def forget_for(db: Session, user_id: int) -> int:
    """Remove a profile's server history (when it opts out)."""
    removed = (
        db.query(WatchEvent)
        .filter(WatchEvent.user_id == user_id, WatchEvent.action == SERVER_WATCHED)
        .delete(synchronize_session=False)
    )
    if removed:
        from app.home_feed import invalidate_discovery

        invalidate_discovery(db, user_id)
    return int(removed or 0)
