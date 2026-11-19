"""Durable Home membership with bounded, snapshot-stable card hydration.

Requests only read published membership and hydrate visible cards. The existing
database doubles as a small durable build queue; no upstream provider is called
by the builder or the serving path. Imports of main are lazy to reuse the legacy
ranking/serialization rules without making startup imports circular.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import timedelta
from time import monotonic, time
from types import SimpleNamespace
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, text, tuple_
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, load_only

from app import library_catalog
from app.sources.availability import annotate_tree
from app.catalog_generator import CatalogGenerator
from app.config import settings
from app.auth import BearerUserKey, invalid_profile_credential
from app.database import get_db, get_db_dependency
from app.models import (
    Account,
    AdminSetting,
    HomeFeedSnapshot,
    HomeFeedState,
    MediaMetadata,
    UniversalCategory,
    UniversalCatalogContent,
    User,
    UserCatalog,
    UserCatalogContent,
    WatchEvent,
)
from app.server_history import TASTE_ACTIONS
from app.time_utils import utc_now

SCHEMA_VERSION = 1
INITIAL_ROWS = 6
PAGE_ITEMS = 20
MAX_BATCH_ROWS = 3
HERO_ITEMS = 8
#: Below this many TMDB votes a title reads as obscure on the billboard.
HERO_MIN_VOTES = 500
#: A hero row needs at least this many qualifying titles to be chosen.
HERO_MIN_ITEMS = 3
#: Bump when the hero rule changes so the worker rebuilds published snapshots
#: (the old membership keeps serving until the new one is published).
HERO_RULE_VERSION = 2
LEASE_SECONDS = 60
RECHECK_SECONDS = 60
RETENTION_HOURS = 24
router = APIRouter()
_POLICY_FIELDS = (
    "enable_personalized_catalogs",
    "enable_universal_catalogs",
    "hide_foreign",
    "hide_adult",
    "hide_unreleased",
    "catalog_size",
    "catalog_shuffle_hours",
    "synth_rotation_days",
)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str).encode()
    ).hexdigest()


def _policy_values(db: Session | None = None) -> dict:
    """Read only Home settings, without sharing mutable config between workers."""
    values = {field: getattr(settings, field) for field in _POLICY_FIELDS}
    if db is not None:
        from app.admin import _bounded

        for key, value in db.query(AdminSetting.key, AdminSetting.value).filter(
            AdminSetting.key.in_([field.upper() for field in _POLICY_FIELDS])
        ):
            field = key.lower()
            try:
                values[field] = (
                    value.lower() in ("true", "1", "yes")
                    if isinstance(values[field], bool)
                    else _bounded(field, int(value))
                )
            except (AttributeError, TypeError, ValueError):
                logger.warning("Ignoring invalid Home policy setting {}", key)
    return values


def _policy_digest(values: dict) -> str:
    return _digest([SCHEMA_VERSION, *(values[field] for field in _POLICY_FIELDS)])


def policy_fingerprint(db: Session | None = None) -> str:
    """Policy changes cannot serve an old snapshot, even during a rebuild."""
    return _policy_digest(_policy_values(db))


def _source_fingerprint(db: Session, user: User, policy: dict | None = None) -> str:
    # This reconciliation runs in the worker, never in GET /home. Counts catch
    # deletions as well as timestamps. Activity is deliberately not part of it:
    # only history/taste changes invalidate discovery, not library/progress ticks.
    universal = (
        db.query(
            func.count(UniversalCatalogContent.tmdb_id),
            func.max(UniversalCatalogContent.last_updated),
            func.max(UniversalCategory.updated_at),
        )
        .select_from(UniversalCategory)
        .outerjoin(
            UniversalCatalogContent,
            UniversalCatalogContent.category_id == UniversalCategory.id,
        )
        .filter(
            UniversalCategory.is_active.is_(True),
            UniversalCategory.source == "generated",
        )
        .first()
    )
    personal = (
        db.query(
            func.count(UserCatalogContent.tmdb_id),
            func.max(UserCatalogContent.added_at),
            func.max(UserCatalog.last_generated),
        )
        .select_from(UserCatalog)
        .outerjoin(
            UserCatalogContent,
            UserCatalogContent.catalog_id == UserCatalog.id,
        )
        .filter(UserCatalog.user_id == user.id, UserCatalog.is_active.is_(True))
        .first()
    )
    history = (
        db.query(func.count(WatchEvent.id), func.max(WatchEvent.created_at))
        .filter(
            WatchEvent.user_id == user.id,
            WatchEvent.action.in_(TASTE_ACTIONS),
        )
        .first()
    )
    policy = policy or _policy_values(db)
    shuffle_hours = policy["catalog_shuffle_hours"]
    rotation_days = policy["synth_rotation_days"]
    return _digest(
        [
            library_catalog.normalize_scope(getattr(user, "content_scope", None)),
            library_catalog.library_fingerprint(db),
            tuple(universal or ()),
            tuple(personal or ()),
            tuple(history or ()),
            user.last_sync,
            int(time()) // (shuffle_hours * 3600) if shuffle_hours > 0 else 0,
            utc_now().date().toordinal() // rotation_days if rotation_days > 0 else 0,
            _policy_digest(policy),
            HERO_RULE_VERSION,
        ]
    )


def ensure_state(db: Session, user_id: int) -> HomeFeedState:
    """Race-safe insertion without committing the caller's transaction."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    state = db.get(HomeFeedState, user_id)
    if state is not None:
        return state
    insert = sqlite_insert if db.bind.dialect.name == "sqlite" else pg_insert
    db.execute(
        insert(HomeFeedState)
        .values(
            user_id=user_id,
            generation=1,
            published_generation=0,
            next_check_at=utc_now(),
        )
        .on_conflict_do_nothing(index_elements=["user_id"])
    )
    return db.get(HomeFeedState, user_id)


def invalidate_discovery(db: Session, user_id: int | None = None) -> None:
    """Mark a source mutation in its own transaction; absent states build fresh."""
    query = db.query(HomeFeedState)
    if user_id is not None:
        query = query.filter(HomeFeedState.user_id == user_id)
    query.update(
        {
            HomeFeedState.generation: HomeFeedState.generation + 1,
            HomeFeedState.next_check_at: utc_now(),
            HomeFeedState.retry_at: None,
        },
        synchronize_session=False,
    )


def invalidate_published_discovery(user_id: int | None = None) -> None:
    """Post-commit bridge for metadata/tag maintenance jobs and admin tools."""
    with get_db() as db:
        invalidate_discovery(db, user_id)


def build_membership(db: Session, user: User, policy: dict | None = None) -> dict:
    """Apply exactly the legacy native ranking and full-page diversity rules."""
    from app import main
    from workers.personalized_sync import get_slot_sort_order, VALID_SLOTS

    policy = policy or _policy_values(db)
    personal = (
        db.query(UserCatalog)
        .filter(
            UserCatalog.user_id == user.id,
            UserCatalog.is_active.is_(True),
            UserCatalog.slot_id.in_(VALID_SLOTS),
        )
        .all()
        if policy["enable_personalized_catalogs"]
        else []
    )
    personal.sort(key=lambda row: get_slot_sort_order(row.slot_id))
    # A tag maintenance job can change taste without changing watch timestamps.
    # A new membership build must not inherit that process-local cached vector.
    main._taste_cache.pop(user.id, None)
    taste, excluded = main._get_user_taste_data(user.id, db)
    generator = CatalogGenerator(db)
    personal_items = generator.get_catalog_contents_batch(
        [row.slot_id for row in personal],
        user_id=user.id,
        hide_foreign=policy["hide_foreign"],
        hide_adult=policy["hide_adult"],
        hide_unreleased=False,
        user_taste_tag_ids=taste,
        exclude_ids=excluded,
        membership_only=True,
        catalog_size=policy["catalog_size"],
    )
    universals = main._universal_row_categories(
        db,
        limit=main.MAX_NATIVE_DISCOVERY_ROWS,
        enabled=policy["enable_universal_catalogs"],
        rotation_days=policy["synth_rotation_days"],
    )
    main._reorder_universals_by_taste(universals, user.id, db)
    universal_items = generator.get_catalog_contents_batch(
        [row.id for row in universals],
        hide_foreign=policy["hide_foreign"],
        hide_adult=policy["hide_adult"],
        hide_unreleased=policy["hide_unreleased"],
        user_taste_tag_ids=taste,
        exclude_ids=excluded,
        membership_only=True,
        catalog_size=policy["catalog_size"],
    )
    rows = []

    def append_row(identifier, title, media_type, items, revision):
        valid = [
            item
            for item in main._shuffle_items(
                items, identifier, shuffle_hours=policy["catalog_shuffle_hours"]
            )
            if item.get("tmdb_id")
        ]
        revisions = {
            item.get("_snapshot_revision")
            for item in valid
            if item.get("_snapshot_revision")
        }
        if revision and revisions and revisions != {revision}:
            return
        rows.append(
            {
                "id": identifier,
                "name": title,
                "media_type": media_type,
                "items": valid[: main.WEB_DASHBOARD_CANDIDATE_LIMIT],
            }
        )

    for row in personal:
        params = (
            row.generation_params if isinstance(row.generation_params, dict) else {}
        )
        # Preserve the BYW label projection, including legacy scalar seed fields.
        label = main._catalog_row_from_items(
            row.name,
            f"personal-{row.slot_id}",
            main._api_media_type(row.media_type),
            [],
            generation_method=row.generation_method,
            generation_params=params,
        )["name"]
        append_row(
            f"personal-{row.slot_id}",
            label,
            row.media_type,
            personal_items.get(row.slot_id, []),
            params.get("generation_revision") or row.last_generated.isoformat(),
        )
    for row in universals:
        append_row(
            f"universal-{row.id}",
            row.name,
            row.media_type,
            universal_items.get(row.id, []),
            row.updated_at.isoformat(),
        )

    # "Not for me" titles are already gone: they are in the taste exclusions.
    scope = library_catalog.normalize_scope(getattr(user, "content_scope", None))
    owned = library_catalog.owned_entries(db)
    if scope == "my_servers":
        rows = library_catalog.filter_rows_to_owned(rows, owned)
    rows = main._assemble_page(
        rows,
        item_limit=main.NATIVE_DASHBOARD_ROW_ITEM_LIMIT,
        min_items=(
            library_catalog.MY_SERVERS_MIN_ROW_ITEMS if scope == "my_servers" else None
        ),
    )
    # Library rows lead Home and sit outside the page's de-duplication: owning
    # a title must not take it away from a discovery row (or vice versa).
    rows = library_catalog.compose_rows(db, user, scope, owned) + rows
    hero_row, hero_members = _select_hero(rows)

    def identity(item):
        return [item["media_type"], item["tmdb_id"]]

    def snapshot_row(row):
        out = {
            "id": row["id"],
            "title": row["name"],
            "type": row["media_type"],
            "members": [identity(item) for item in row["items"]],
        }
        # Why each title is in the row ("Because you watched Heat"), keyed by
        # "kind:id". Only rows that explain themselves carry it.
        reasons = {
            f"{item['media_type']}:{item['tmdb_id']}": item["reason"]
            for item in row["items"]
            if item.get("reason")
        }
        if reasons:
            out["reasons"] = reasons
        return out

    return {
        "rows": [snapshot_row(row) for row in rows],
        "hero": [identity(item) for item in hero_members],
        "hero_source_row_id": hero_row["id"] if hero_row else None,
    }


def _select_hero(rows: list[dict]) -> tuple[dict | None, list[dict]]:
    """Choose the billboard row and its titles.

    The billboard renders a title logo over landscape art, so a candidate needs
    both, and enough TMDB votes not to read as obscure. Rows are tried in hero
    preference order (Top Picks first). A personal row full of niche titles
    therefore yields to Trending rather than filling the billboard with text
    titles. The bar relaxes (logo only, then the old rule) only when no row
    clears it, because a Home with no hero is worse than a weaker one.
    """
    from app import main

    preferred = sorted(
        (
            row
            for row in rows
            if main._hero_row_rank(row) < len(main._HERO_ROW_PREFERENCE)
        ),
        key=main._hero_row_rank,
    )

    def presentable(item):
        return bool(item.get("logo") and item.get("backdrop"))

    def well_known(item):
        return presentable(item) and (item.get("_votes") or 0) >= HERO_MIN_VOTES

    for test in (well_known, presentable):
        for row in preferred:
            members = [item for item in row["items"] if test(item)]
            if len(members) >= HERO_MIN_ITEMS:
                return row, members[:HERO_ITEMS]

    hero_row = next(iter(preferred), next(iter(rows), None))
    if not hero_row:
        return None, []
    # Prefer landscape art, while retaining useful candidates if none exists.
    members = [item for item in hero_row["items"] if item.get("backdrop")]
    return hero_row, (members or hero_row["items"])[:HERO_ITEMS]


def _claim_next(db: Session) -> dict | None:
    now = utc_now()
    candidates = (
        db.query(HomeFeedState.user_id)
        .join(User, User.id == HomeFeedState.user_id)
        .join(Account, Account.id == User.account_id)
        .filter(
            User.is_active.is_(True),
            Account.is_active.is_(True),
            or_(HomeFeedState.lease_until.is_(None), HomeFeedState.lease_until <= now),
            or_(HomeFeedState.retry_at.is_(None), HomeFeedState.retry_at <= now),
            or_(
                HomeFeedState.snapshot_id.is_(None),
                HomeFeedState.generation != HomeFeedState.published_generation,
                HomeFeedState.next_check_at <= now,
            ),
        )
        .order_by(HomeFeedState.requested_at.desc().nullslast(), HomeFeedState.user_id)
        .limit(8)
        .all()
    )
    for (user_id,) in candidates:
        # Another builder, or a sync's final invalidation, may hold this row.
        # Move on to the next candidate instead of waiting for that commit.
        if (
            db.query(HomeFeedState.user_id)
            .filter(HomeFeedState.user_id == user_id)
            .with_for_update(skip_locked=True)
            .first()
            is None
        ):
            continue
        token = str(uuid4())
        changed = (
            db.query(HomeFeedState)
            .filter(
                HomeFeedState.user_id == user_id,
                or_(
                    HomeFeedState.lease_until.is_(None),
                    HomeFeedState.lease_until <= now,
                ),
                or_(HomeFeedState.retry_at.is_(None), HomeFeedState.retry_at <= now),
                or_(
                    HomeFeedState.snapshot_id.is_(None),
                    HomeFeedState.generation != HomeFeedState.published_generation,
                    HomeFeedState.next_check_at <= now,
                ),
            )
            .update(
                {
                    "lease_token": token,
                    "lease_until": now + timedelta(seconds=LEASE_SECONDS),
                },
                synchronize_session=False,
            )
        )
        if not changed:
            continue
        db.flush()
        db.expire_all()
        state = db.get(HomeFeedState, user_id)
        result = {
            "user_id": user_id,
            "token": token,
            "generation": state.generation,
            "published_generation": state.published_generation,
            "snapshot_id": state.snapshot_id,
        }
        db.commit()  # The build never holds a queue lock or a request Session.
        return result
    return None


def _finish_claim(
    db: Session, claim: dict, membership: dict | None, source: str, policy: str
) -> bool:
    now = utc_now()
    state = (
        db.query(HomeFeedState)
        .filter(
            HomeFeedState.user_id == claim["user_id"],
            HomeFeedState.lease_token == claim["token"],
            HomeFeedState.generation == claim["generation"],
        )
        .with_for_update()
        .first()
    )
    if state is None:
        # Release only our own obsolete lease; never another worker's claim.
        db.query(HomeFeedState).filter(
            HomeFeedState.user_id == claim["user_id"],
            HomeFeedState.lease_token == claim["token"],
        ).update({"lease_token": None, "lease_until": None}, synchronize_session=False)
        return False
    if membership is not None:
        snapshot_id = str(uuid4())
        db.add(
            HomeFeedSnapshot(
                id=snapshot_id,
                user_id=claim["user_id"],
                schema_version=SCHEMA_VERSION,
                policy_fingerprint=policy,
                source_fingerprint=source,
                membership=membership,
                created_at=now,
            )
        )
        if state.snapshot_id:
            db.query(HomeFeedSnapshot).filter(
                HomeFeedSnapshot.id == state.snapshot_id
            ).update({"superseded_at": now}, synchronize_session=False)
        state.snapshot_id = snapshot_id
    state.published_generation = claim["generation"]
    state.lease_token = None
    state.lease_until = None
    state.retry_at = None
    state.next_check_at = now + timedelta(seconds=RECHECK_SECONDS)
    return True


def build_next() -> bool:
    """One cross-process lease/build/publication, with crash-safe retry state."""
    with get_db() as db:
        claim = _claim_next(db)
    if claim is None:
        return False
    started = monotonic()
    try:
        with get_db() as db:
            user = db.get(User, claim["user_id"])
            if user is None or not user.is_active:
                return True
            policy_values = _policy_values(db)
            source = _source_fingerprint(db, user, policy_values)
            policy = _policy_digest(policy_values)
            previous = (
                db.get(HomeFeedSnapshot, claim["snapshot_id"])
                if claim["snapshot_id"]
                else None
            )
            reusable = (
                previous is not None
                and previous.schema_version == SCHEMA_VERSION
                and previous.source_fingerprint == source
                and previous.policy_fingerprint == policy
                and claim["generation"] == claim["published_generation"]
            )
            membership = None if reusable else build_membership(db, user, policy_values)
            # Re-read publication timestamps under READ COMMITTED. Concurrent
            # source changes must not publish mixed membership under a fresh tag.
            db.expire_all()
            current_policy = _policy_values(db)
            if (
                _source_fingerprint(db, db.get(User, claim["user_id"]), current_policy)
                != source
                or _policy_digest(current_policy) != policy
            ):
                invalidate_discovery(db, claim["user_id"])
                _finish_claim(db, claim, None, source, policy)
                return True
            published = _finish_claim(db, claim, membership, source, policy)
            logger.info(
                "home_snapshot profile={} published={} reused={} duration_ms={:.1f}",
                claim["user_id"],
                published,
                reusable,
                (monotonic() - started) * 1000,
            )
    except Exception:
        logger.exception(
            "Home membership build failed for profile {}", claim["user_id"]
        )
        with get_db() as db:
            db.query(HomeFeedState).filter(
                HomeFeedState.user_id == claim["user_id"],
                HomeFeedState.lease_token == claim["token"],
            ).update(
                {
                    "lease_token": None,
                    "lease_until": None,
                    "retry_at": utc_now() + timedelta(seconds=30),
                },
                synchronize_session=False,
            )
    return True


def backfill_states() -> None:
    with get_db() as db:
        missing = (
            db.query(User.id)
            .join(Account, Account.id == User.account_id)
            .outerjoin(HomeFeedState, HomeFeedState.user_id == User.id)
            .filter(
                User.is_active.is_(True),
                Account.is_active.is_(True),
                HomeFeedState.user_id.is_(None),
            )
            .all()
        )
        for (user_id,) in missing:
            ensure_state(db, user_id)


def prune_snapshots(db: Session) -> int:
    return (
        db.query(HomeFeedSnapshot)
        .filter(
            HomeFeedSnapshot.superseded_at
            < utc_now() - timedelta(hours=RETENTION_HOURS),
            ~HomeFeedSnapshot.id.in_(
                db.query(HomeFeedState.snapshot_id).filter(
                    HomeFeedState.snapshot_id.isnot(None)
                )
            ),
        )
        .delete(synchronize_session=False)
    )


async def run_home_feed_scheduler() -> None:
    """Two off-loop builders share durable leases with other application workers."""

    from app.job_threads import run_job_thread

    async def worker():
        while True:
            try:
                worked = await run_job_thread(build_next)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Home membership queue unavailable")
                worked = False
            await asyncio.sleep(0.05 if worked else 1)

    async def reconcile():
        while True:
            try:
                await run_job_thread(backfill_states)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Home membership backfill failed")
            await asyncio.sleep(60)

    await asyncio.gather(reconcile(), worker(), worker())


def _authorize(db: Session, user_key: str) -> User:
    user = db.query(User).filter(User.user_key == user_key).first()
    if user is None:
        raise invalid_profile_credential()
    account = db.get(Account, user.account_id) if user.account_id else None
    if not user.is_active or account is None or not account.is_active:
        raise HTTPException(403, "Profile or account is inactive")
    return user


def _compatible_snapshot(snapshot: HomeFeedSnapshot | None, policy: str) -> bool:
    return bool(
        snapshot
        and snapshot.schema_version == SCHEMA_VERSION
        and snapshot.policy_fingerprint == policy
    )


def _valid_snapshot(snapshot: HomeFeedSnapshot | None, policy: str) -> bool:
    return bool(
        _compatible_snapshot(snapshot, policy)
        and (
            snapshot.superseded_at is None
            or snapshot.superseded_at >= utc_now() - timedelta(hours=RETENTION_HOURS)
        )
    )


def _cached_policy_invalid(
    db: Session,
    user_id: int,
    snapshot: HomeFeedSnapshot | None,
    policy: str,
    header: str | None,
) -> bool:
    """A replacement may be ready while the client still renders old membership."""
    identifiers = set()
    for token in (header or "").split(","):
        parts = token.strip().removeprefix("W/").strip('"').split(":")
        if not parts[0].startswith("home-v"):
            continue
        if len(parts) != 3 or parts[0] != f"home-v{SCHEMA_VERSION}":
            return True
        if snapshot is not None and parts[1] == snapshot.id:
            if not _compatible_snapshot(snapshot, policy):
                return True
        else:
            identifiers.add(parts[1])
    if not identifiers:
        return False
    # Bound a caller-supplied ETag list as strictly as a continuation batch.
    if len(identifiers) > 8:
        return True
    retained = (
        db.query(
            HomeFeedSnapshot.id,
            HomeFeedSnapshot.schema_version,
            HomeFeedSnapshot.policy_fingerprint,
        )
        .filter(
            HomeFeedSnapshot.user_id == user_id,
            HomeFeedSnapshot.id.in_(identifiers),
        )
        .all()
    )
    # Unknown/pruned tags cannot prove the rendered cache is policy-compatible.
    return len(retained) != len(identifiers) or any(
        version != SCHEMA_VERSION or fingerprint != policy
        for _, version, fingerprint in retained
    )


def _hydrate(db: Session, members: list[list]) -> dict[tuple, dict]:
    """The only serving metadata query; its input is already sliced and bounded."""
    from app import main

    keys = list(dict.fromkeys((kind, int(identifier)) for kind, identifier in members))
    if not keys:
        return {}
    metas = (
        db.query(MediaMetadata)
        .options(
            load_only(
                *main._MEDIA_CARD_COLUMNS,
                MediaMetadata.rating_consensus,
                MediaMetadata.vote_count,
            )
        )
        .filter(tuple_(MediaMetadata.media_type, MediaMetadata.tmdb_id).in_(keys))
        .all()
    )
    metadata = {(meta.media_type, meta.tmdb_id): meta for meta in metas}
    cards = {}
    for kind, identifier in keys:
        content = SimpleNamespace(
            tmdb_id=identifier, media_type=kind, rank=1, match_score=0
        )
        item = CatalogGenerator._content_item(content, metadata.get((kind, identifier)))
        row = main._catalog_row_from_items(
            "", "home", main._api_media_type(kind), [item], limit=1, shuffle=False
        )
        cards[(kind, identifier)] = main._card_projected_rows([row])[0]["items"][0]
    return cards


def _with_reason(card: dict, reason: str | None) -> dict:
    return {**card, "reason": reason} if reason else card


def _page(row: dict, cursor: int, limit: int, cards: dict) -> dict:
    reasons = row.get("reasons") or {}
    start = min(cursor, len(row["members"]))
    end = min(start + limit, len(row["members"]))
    return {
        "id": row["id"],
        "title": row["title"],
        "type": row["type"],
        "items": [
            _with_reason(cards[(kind, identifier)], reasons.get(f"{kind}:{identifier}"))
            for kind, identifier in row["members"][start:end]
        ],
        "cursor": start,
        "next_cursor": end if end < len(row["members"]) else None,
        "total": len(row["members"]),
    }


class HomeRowRequest(BaseModel):
    id: str = Field(min_length=1, max_length=200)
    cursor: int = Field(default=0, ge=0, le=80)
    limit: int = Field(default=PAGE_ITEMS, ge=1, le=PAGE_ITEMS)


class HomeRowsRequest(BaseModel):
    snapshot_id: str = Field(min_length=1, max_length=36)
    rows: list[HomeRowRequest] = Field(min_length=1, max_length=MAX_BATCH_ROWS)


REQUEST_HINT_SECONDS = 10
REQUEST_HINT_LOCK_TIMEOUT = "500ms"


def _note_home_request(
    db: Session, user_id: int, state: HomeFeedState, *, due_now: bool
) -> None:
    """Record demand for the builders without ever blocking the response.

    ``requested_at`` only orders the build queue, so it is refreshed at most every
    few seconds; ``due_now`` also pulls the next check forward. A sync or builder
    holding this row's lock used to stall the request until that transaction
    committed (55 s observed); now the hint is skipped after a short lock wait.
    """
    now = utc_now()
    requested_at = state.requested_at
    fresh = requested_at is not None and (now - requested_at) < timedelta(
        seconds=REQUEST_HINT_SECONDS
    )
    if fresh and not due_now:
        return
    values = {"requested_at": now}
    if due_now:
        values["next_check_at"] = now
    try:
        with db.begin_nested():
            if db.bind is not None and db.bind.dialect.name == "postgresql":
                db.execute(
                    text(f"SET LOCAL lock_timeout = '{REQUEST_HINT_LOCK_TIMEOUT}'")
                )
            db.query(HomeFeedState).filter(HomeFeedState.user_id == user_id).update(
                values, synchronize_session=False
            )
        db.commit()
    except OperationalError as exc:
        db.rollback()
        logger.info(f"home_request_hint_skipped user_id={user_id} reason={exc.orig}")


@router.get("/api/v2/home")
def native_home(
    request: Request, user_key: BearerUserKey, db: Session = Depends(get_db_dependency)
):
    from app.main import _if_none_match_hit, _schedule_local_sync

    started = monotonic()
    user = _authorize(db, user_key)
    policy = policy_fingerprint(db)
    if user.last_sync is None and user.auth_source == "local":
        _schedule_local_sync(user.id)
    state = ensure_state(db, user.id)
    db.commit()
    snapshot = (
        db.get(HomeFeedSnapshot, state.snapshot_id) if state.snapshot_id else None
    )
    if _cached_policy_invalid(
        db, user.id, snapshot, policy, request.headers.get("if-none-match")
    ):
        _note_home_request(
            db, user.id, state, due_now=not _valid_snapshot(snapshot, policy)
        )
        raise HTTPException(412, "policy_invalidated")
    if not _valid_snapshot(snapshot, policy):
        _note_home_request(db, user.id, state, due_now=True)
        # Tell a client explicitly to evict incompatible cached membership before
        # it retries. A bare 202 would otherwise leave its old-policy cards visible.
        if (
            snapshot is not None
            and not _compatible_snapshot(snapshot, policy)
            and request.headers.get("if-none-match")
        ):
            raise HTTPException(412, "policy_invalidated")
        return JSONResponse(
            status_code=202,
            content={
                "snapshot_pending": True,
                "retry_after": 1,
                "policy_fingerprint": policy,
            },
            headers={"Retry-After": "1", "Cache-Control": "private, no-store"},
        )
    _note_home_request(db, user.id, state, due_now=False)
    membership = snapshot.membership
    rows = membership["rows"]
    members = [
        member for row in rows[:INITIAL_ROWS] for member in row["members"][:PAGE_ITEMS]
    ]
    cards = _hydrate(db, members + membership["hero"])
    payload = {
        "schema_version": SCHEMA_VERSION,
        "snapshot_id": snapshot.id,
        "discovery_revision": snapshot.id,
        "row_descriptors": [
            {
                "id": row["id"],
                "title": row["title"],
                "type": row["type"],
                "total": len(row["members"]),
            }
            for row in rows
        ],
        "hero_candidates": [
            cards[(kind, identifier)] for kind, identifier in membership["hero"]
        ],
        "hero_source_row_id": membership["hero_source_row_id"],
        "rows": [_page(row, 0, PAGE_ITEMS, cards) for row in rows[:INITIAL_ROWS]],
    }
    # Ownership is computed per request (never stored in the snapshot) and is
    # part of the hashed body, so a library sync changes the ETag.
    annotate_tree(db, payload)
    # Cards are hydrated from current metadata. Hash the bounded representation,
    # not only membership, so a poster/title correction cannot receive a false
    # 304 while retaining the same discovery order. This still never builds rows.
    etag = f'"home-v{SCHEMA_VERSION}:{snapshot.id}:{_digest(payload)[:24]}"'
    headers = {
        "ETag": etag,
        "Cache-Control": "private, no-cache",
        "Server-Timing": f'home;dur={(monotonic() - started) * 1000:.1f};desc="published snapshot"',
    }
    if _if_none_match_hit(request, etag):
        return Response(status_code=304, headers=headers)
    return JSONResponse(content=payload, headers=headers)


@router.post("/api/v2/home/rows")
def native_home_rows(
    user_key: BearerUserKey,
    payload: HomeRowsRequest,
    db: Session = Depends(get_db_dependency),
):
    started = monotonic()
    user = _authorize(db, user_key)
    policy = policy_fingerprint(db)
    snapshot = (
        db.query(HomeFeedSnapshot)
        .filter(
            HomeFeedSnapshot.id == payload.snapshot_id,
            HomeFeedSnapshot.user_id == user.id,
        )
        .first()
    )
    if snapshot is not None and not _compatible_snapshot(snapshot, policy):
        raise HTTPException(412, "policy_invalidated")
    if not _valid_snapshot(snapshot, policy):
        raise HTTPException(409, "snapshot_expired")
    rows = {row["id"]: row for row in snapshot.membership["rows"]}
    if len({row.id for row in payload.rows}) != len(payload.rows):
        raise HTTPException(422, "Duplicate row request")
    if any(row.id not in rows for row in payload.rows):
        raise HTTPException(422, "Unknown row in snapshot")
    if any(row.cursor > len(rows[row.id]["members"]) for row in payload.rows):
        raise HTTPException(422, "Cursor exceeds row membership")
    members = [
        member
        for req in payload.rows
        for member in rows[req.id]["members"][req.cursor : req.cursor + req.limit]
    ]
    cards = _hydrate(db, members)
    annotate_tree(db, list(cards.values()))
    return JSONResponse(
        content={
            "snapshot_id": snapshot.id,
            "rows": [
                _page(rows[req.id], req.cursor, req.limit, cards)
                for req in payload.rows
            ],
        },
        headers={
            "Cache-Control": "private, no-store",
            "Server-Timing": f"home_cards;dur={(monotonic() - started) * 1000:.1f}",
        },
    )
