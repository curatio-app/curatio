"""
Sync MDBList data: bulk ratings, per-title facts, and curated lists.

    # per-title facts AND ratings: keywords, certification, age rating, reviews
    python workers/mdb_sync.py --enrich --limit 50000

    # optional: extra ratings from named lists you know exist. Not needed for
    # coverage -- --enrich stores ratings from the same payload as the facts.
    python workers/mdb_sync.py --ratings --rating-lists best-neo-noir

    # curated lists, as a tagging cross-check
    python workers/mdb_sync.py --add-list best-neo-noir --tag-hint "Neo-Noir"
    python workers/mdb_sync.py --sync-lists
    python workers/mdb_sync.py --audit-lists

    python workers/mdb_sync.py --status

Everything here is optional. With no key stored in ``admin_settings`` every
command reports that and exits 0 -- ratings fall back to TMDB's ``vote_average``
and the tagging prompt simply omits the MDBList block.

Ordered by popularity descending, so an interrupted pass leaves the part of the
library that actually appears in catalogs done.
"""

import argparse
import asyncio
import sys
from datetime import timedelta
from pathlib import Path

from loguru import logger
from sqlalchemy import func

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.time_utils import utc_now  # noqa: E402
from app.config import settings  # noqa: E402
from app.database import get_db  # noqa: E402
from app.mdblist_client import (  # noqa: E402
    MDBListClient,
    consensus,
    get_api_key,
)
from workers.build_control import stop_event  # noqa: E402
from app.models import (  # noqa: E402
    MdbList,
    MdbListItem,
    MediaMetadata,
    MovieTag,
    Tag,
    TitleRating,
)

#: Bumped whenever the MDBList request shape changes in a way that makes
#: previously-stored facts incomplete. 1.1.0 is the first version to request
#: append_to_response, i.e. the first whose mdb_facts can contain reviews.
#: 1.2.0 invalidates rows written before batch results were matched through
#: ``ids.tmdb`` rather than MDBList's unrelated top-level ``id``. Those rows
#: can contain another title's facts and must be fetched again once.
MDB_VERSION = "1.2.0"

#: Stamped on titles MDBList has no record of, so they leave the selection query
#: instead of being re-requested on every run. Deliberately distinct from
#: MDB_VERSION: a bump to the latter revisits them, a reader can tell "absent"
#: from "enriched", and nothing mistakes the sentinel for real facts.
MDB_ABSENT_VERSION = f"{MDB_VERSION}-absent"

#: The shipped default for ``mdblist_batch_size``. When the setting still holds
#: it, the batch size is taken from whatever the key allows rather than from the
#: setting -- see the note in :func:`enrich_titles`.
DEFAULT_BATCH_SIZE = 10

#: Titles that must be attempted before the yield check can fire. Large enough
#: that a handful of genuinely-unknown titles cannot trip it, small enough that a
#: systematic shape mismatch is caught in seconds rather than hours.
_YIELD_SAMPLE = 500
#: Below this share of attempted titles actually stored, treat it as a shape
#: mismatch rather than bad luck. The broken runs sat at 0.007.
_MIN_YIELD = 0.05


def _client(db) -> MDBListClient | None:
    key = get_api_key(db)
    if not key:
        logger.warning(
            "No MDBList API key configured. Set it in the admin dashboard; "
            "ratings will fall back to TMDB until then."
        )
        return None
    client = MDBListClient(key, db)
    client.load_quota()
    return client


def _payload_tmdb_id(payload: dict) -> int | None:
    """The TMDB id a batch item corresponds to.

    Batch responses come back as an array with no guaranteed correspondence to
    request order, so items are matched by id rather than by position -- getting
    that wrong would write one title's reviews onto another.

    **``ids.tmdb`` is the only authoritative source, and a bare ``id`` is not a
    TMDB id at all.** A live probe settles it:

        {"id": 469990, "title": "The Matrix",
         "ids": {"imdb": "tt0133093", "trakt": 481, "tmdb": 603, "tvdb": 169}}

    ``id`` is MDBList's own primary key. This function used to try
    ``("tmdb_id", "tmdbid", "id")`` before looking inside ``ids``, so every title
    resolved to MDBList's id, matched no ``media_metadata`` row, and was dropped
    -- silently, because the caller had already counted it as returned. Two full
    production runs stored ~0.7% of what they fetched. The ones that landed were
    coincidences where an MDBList id happened to equal a real TMDB id.

    Confirmed identical for shows (Breaking Bad: ``id`` 406423, ``ids.tmdb``
    1396), so there is no per-type special case.
    """
    ids = (payload or {}).get("ids")
    if isinstance(ids, dict):
        try:
            return int(ids["tmdb"])
        except (KeyError, TypeError, ValueError):
            pass
    # Tolerated only because they are unambiguous by name. `id` is deliberately
    # absent: an ambiguous key that silently resolves to the wrong row is worse
    # than no key, which is the entire lesson of this function.
    for key in ("tmdb_id", "tmdbid"):
        value = (payload or {}).get(key)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _store_ratings(db, tmdb_id: int, media_type: str, ratings: list[dict]) -> int:
    """Replace a title's rating rows and recompute its consensus."""
    db.query(TitleRating).filter(
        TitleRating.tmdb_id == tmdb_id, TitleRating.media_type == media_type
    ).delete(synchronize_session=False)

    now = utc_now()
    pairs: list[tuple[float, int | None]] = []
    for entry in ratings:
        db.add(
            TitleRating(
                tmdb_id=tmdb_id,
                media_type=media_type,
                source=entry["source"],
                value=entry.get("value"),
                normalized=entry["normalized"],
                votes=entry.get("votes"),
                fetched_at=now,
            )
        )
        pairs.append((entry["normalized"], entry.get("votes")))

    score, count = consensus(pairs)
    row = (
        db.query(MediaMetadata)
        .filter(
            MediaMetadata.tmdb_id == tmdb_id, MediaMetadata.media_type == media_type
        )
        .first()
    )
    if row is not None:
        row.rating_consensus = score
        row.rating_source_count = count
    return count


async def sync_ratings(db, client: MDBListClient, slugs: list[str]) -> dict:
    """Stage 1 — bulk ratings from list endpoints.

    Cheap: a handful of requests covers whatever those lists contain.

    Coverage is NOT guaranteed to be the whole library. List endpoints page
    through *lists*, not the full MDBList catalog, and per-list caps run
    30,000-150,000 by tier, so no single list holds a 354k library. Measure what
    lands rather than assuming; stage 2 backfills the rest at one request per
    uncovered title.
    """
    stats = {"requests": 0, "titles": 0, "rated": 0, "stopped": False}
    for slug in slugs:
        offset = 0
        while True:
            page = await client.list_items(slug, limit=1000, offset=offset)
            stats["requests"] += 1
            if not page:
                break
            items = page if isinstance(page, list) else page.get("items") or []
            if not items:
                break
            for item in items:
                tmdb_id = item.get("tmdb_id") or item.get("id")
                if not tmdb_id:
                    continue
                media_type = "tv" if item.get("mediatype") == "show" else "movie"
                ratings = client.extract_ratings(item)
                stats["titles"] += 1
                if ratings:
                    _store_ratings(db, int(tmdb_id), media_type, ratings)
                    stats["rated"] += 1
            db.commit()
            if len(items) < 1000:
                break
            offset += 1000
    logger.info(
        f"ratings: {stats['requests']} requests, {stats['titles']:,} titles seen, "
        f"{stats['rated']:,} with ratings"
    )
    return stats


async def enrich_titles(
    db,
    client: MDBListClient,
    limit: int | None,
    media_type: str | None,
    on_progress=None,
) -> dict:
    """Stage 2 — per-title facts, concurrency-bounded and quota-aware."""
    query = db.query(MediaMetadata.tmdb_id, MediaMetadata.media_type).order_by(
        MediaMetadata.popularity.desc().nullslast()
    )
    if media_type:
        query = query.filter(MediaMetadata.media_type == media_type)
    # Version, not just presence. "mdb_enriched_at IS NULL" meant a title
    # fetched by an older client was skipped forever -- so the rows fetched
    # before the request asked for reviews would have kept their review-free
    # facts permanently. Keying off the version re-fetches exactly those and
    # nothing else; TMDB enrichment is untouched.
    #
    # MDB_ABSENT_VERSION is excluded alongside MDB_VERSION: it is not the
    # current version, so a plain `!= MDB_VERSION` would re-select every title
    # already known to be missing from MDBList and the sentinel would do
    # nothing. Bumping MDB_VERSION changes both constants, which is what makes
    # a version bump revisit them deliberately.
    done_versions = (MDB_VERSION, MDB_ABSENT_VERSION)
    stale = (MediaMetadata.mdb_version.is_(None)) | (
        MediaMetadata.mdb_version.notin_(done_versions)
    )
    if settings.mdblist_refresh_days > 0:
        cutoff = utc_now() - timedelta(days=settings.mdblist_refresh_days)
        query = query.filter(stale | (MediaMetadata.mdb_enriched_at < cutoff))
    else:
        query = query.filter(stale)
    if limit:
        query = query.limit(limit)
    targets = [(int(r[0]), r[1]) for r in query.all()]

    logger.info(f"mdblist enrich: {len(targets):,} titles")
    if not targets:
        return {
            "enriched": 0,
            "rated": 0,
            "unmatched_payload": 0,
            "no_local_row": 0,
            "missed": 0,
            "absent": 0,
            "deferred": 0,
            "stopped": False,
        }

    semaphore = asyncio.Semaphore(settings.mdblist_concurrency)
    # Every bucket a requested id can land in, so they sum to the target count.
    # Collapsing them is what let two runs store 0.7% of what they fetched while
    # logging steady progress and reporting a clean finish:
    #
    #   enriched          written to media_metadata
    #   unmatched_payload came back with no resolvable TMDB id
    #   no_local_row      id resolved, but no media_metadata row has it
    #   missed            requested and not returned at all
    #   absent            requested, not returned, and marked so we stop asking
    #   deferred          never requested (quota wall or stop)
    stats: dict = {
        "enriched": 0,
        "rated": 0,
        "unmatched_payload": 0,
        "no_local_row": 0,
        "missed": 0,
        "absent": 0,
        "deferred": 0,
        "stopped": False,
    }

    # One request per title was what made this a ~10 hour stage. The batch
    # endpoint resolves many ids at once and returns ratings in the same
    # payload, which also removes the need for the separate ratings pass.
    #
    # The cap comes from /user: 100 for a supporter key, 10 otherwise. This was
    # `min(setting, cap)`, which can only ever LOWER the batch -- so a key
    # reporting batch_cap=100 still ran at the setting's conservative default of
    # 10 and the tenfold reduction in requests never happened. Observed in
    # production as "supporter=True batch_cap=100" immediately followed by
    # "2,778 batch requests of up to 10".
    #
    # The setting is a default, not a ceiling: at DEFAULT_BATCH_SIZE it means
    # "whatever the key safely allows", and any other value is a deliberate
    # operator choice that is respected but still clamped to what the key takes.
    if settings.mdblist_batch_size == DEFAULT_BATCH_SIZE:
        batch_size = client.max_batch_ids
    else:
        batch_size = min(settings.mdblist_batch_size, client.max_batch_ids)
    batch_size = max(1, batch_size)
    if batch_size != settings.mdblist_batch_size:
        logger.info(
            f"mdblist: batching {batch_size} ids per request "
            f"(key allows {client.max_batch_ids}, setting is "
            f"{settings.mdblist_batch_size})"
        )
    by_type: dict[str, list[int]] = {"movie": [], "tv": []}
    for tmdb_id, mtype in targets:
        by_type.setdefault(mtype, []).append(tmdb_id)

    async def fetch(ids: list[int], mtype: str) -> tuple[str, list[int], list[dict]]:
        # The requested ids come back with the results so the caller can tell
        # which ones MDBList had no record of, rather than only counting them.
        async with semaphore:
            return mtype, ids, await client.title_info_batch(ids, mtype)

    jobs: list[tuple[list[int], str]] = []
    for mtype, ids in by_type.items():
        for start in range(0, len(ids), batch_size):
            jobs.append((ids[start : start + batch_size], mtype))

    logger.info(
        f"mdblist enrich: {len(targets):,} titles in {len(jobs):,} batch requests "
        f"of up to {batch_size}"
    )

    done = 0
    group = max(1, settings.mdblist_concurrency)
    for offset in range(0, len(jobs), group):
        if stop_event.is_set():
            logger.warning(f"mdblist: stop requested at {done:,}/{len(targets):,}")
            stats["stopped"] = True
            break
        # The client latches _exhausted on a spent quota or a run of 429s, after
        # which _blocked() refuses every request -- and because a refused request
        # never increments _used_today, `remaining` freezes and the check below
        # can never fire. Without this the loop walked the remaining targets in
        # about a second, each returning [], counting them all as `missed` and
        # logging progress the whole way: a run that stopped at 2.4% of the
        # library reported itself complete with 'missed': 274232.
        if client._exhausted:
            logger.warning(
                f"mdblist: client stopped at {done:,}/{len(targets):,}; "
                f"{len(targets) - done:,} deferred to the next run"
            )
            break
        if client.remaining is not None and client.remaining <= 0:
            logger.warning("mdblist: quota reserve reached; resume tomorrow")
            break

        window = jobs[offset : offset + group]
        results = await asyncio.gather(*(fetch(ids, m) for ids, m in window))
        now = utc_now()

        requested = sum(len(ids) for ids, _ in window)
        returned = 0
        for mtype, asked_ids, items in results:
            resolved: set[int] = set()
            for payload in items:
                returned += 1
                tmdb_id = _payload_tmdb_id(payload)
                if tmdb_id is None:
                    stats["unmatched_payload"] += 1
                    continue
                row = (
                    db.query(MediaMetadata)
                    .filter(
                        MediaMetadata.tmdb_id == tmdb_id,
                        MediaMetadata.media_type == mtype,
                    )
                    .first()
                )
                if row is None:
                    # Counted, not swallowed. This branch is where two entire
                    # runs went: every payload resolved to MDBList's own id, so
                    # every lookup missed and every title was dropped in silence.
                    stats["no_local_row"] += 1
                    continue
                resolved.add(tmdb_id)
                row.mdb_facts = client.extract_facts(payload)
                row.mdb_enriched_at = now
                row.mdb_version = MDB_VERSION
                ratings = client.extract_ratings(payload)
                if ratings:
                    _store_ratings(db, tmdb_id, mtype, ratings)
                    stats["rated"] += 1
                stats["enriched"] += 1

            # Anything asked for and not returned is absent from MDBList, and
            # asking again will not change that. Without a marker these rows
            # stay in the target set forever: 27,776 of them, averaging 0.4
            # popularity, re-requested on every run. Stamping a sentinel version
            # takes them out of the selection query while keeping them
            # distinguishable from a success, so bumping MDB_VERSION revisits
            # them deliberately if MDBList's catalogue later grows.
            absent = [i for i in asked_ids if i not in resolved]
            if absent:
                marked = (
                    db.query(MediaMetadata)
                    .filter(
                        MediaMetadata.tmdb_id.in_(absent),
                        MediaMetadata.media_type == mtype,
                        # Never overwrite a real enrichment: a title resolved in
                        # an earlier window keeps its facts.
                        (MediaMetadata.mdb_version.is_(None))
                        | (MediaMetadata.mdb_version != MDB_VERSION),
                    )
                    .update(
                        {MediaMetadata.mdb_version: MDB_ABSENT_VERSION},
                        synchronize_session=False,
                    )
                )
                stats["absent"] += marked
        stats["missed"] += max(0, requested - returned)
        db.commit()

        done += requested
        # ENRICHED, not requested. The old line printed `done`, which advances by
        # the window size whether or not a single row was written -- so a run
        # storing nothing logged identical, reassuring progress all the way to a
        # clean "complete". A progress counter that cannot report failure is
        # worse than none.
        logger.info(
            f"mdblist enrich: {stats['enriched']:,} enriched of "
            f"{min(done, len(targets)):,} attempted / {len(targets):,} target "
            f"(quota left: {client.remaining})"
        )
        if on_progress:
            try:
                on_progress(stats["enriched"], min(done, len(targets)), len(targets))
            except Exception:  # noqa: BLE001 - progress must never fail the run
                pass

        # Fail fast on a run that is storing nothing -- but say WHICH of the two
        # very different causes it is, because they need opposite responses:
        #
        #   unmatched_payload / no_local_row high  -> a real shape mismatch. The
        #       code is reading the wrong field; stop and fix it.
        #   missed high, the other two zero        -> MDBList simply has no
        #       record of these titles. Nothing is broken.
        #
        # The first version of this message asserted "the response shape does
        # not match" unconditionally, and duly printed it against a run whose
        # shape counters were both zero -- sending the reader after a bug that
        # did not exist while the actual signal (missed) went unprinted.
        unusable = stats["unmatched_payload"] + stats["no_local_row"]
        if done >= _YIELD_SAMPLE and stats["enriched"] <= done * _MIN_YIELD:
            if unusable > stats["missed"]:
                cause = (
                    f"{stats['unmatched_payload']:,} payloads carried no TMDB "
                    f"id and {stats['no_local_row']:,} resolved to ids not in "
                    "media_metadata — the response shape does not match what "
                    "this code reads, which is a bug to fix rather than a "
                    "retry to make"
                )
            else:
                cause = (
                    f"{stats['missed']:,} titles were requested and not "
                    "returned at all — MDBList has no record of them. Nothing "
                    "is broken; they are being marked so future runs skip them"
                )
            logger.error(
                f"mdblist: STOPPING — {stats['enriched']:,} enriched "
                f"from {done:,} attempted. {cause}."
            )
            break

    stats["deferred"] = max(0, len(targets) - done)
    client._persist_quota()
    if stats["deferred"]:
        logger.warning(
            f"mdblist enrich INCOMPLETE: {stats['enriched']:,} enriched, "
            f"{stats['deferred']:,} of {len(targets):,} never attempted — "
            "re-run to continue"
        )
    else:
        logger.info(f"mdblist enrich complete: {stats}")
    if stats["absent"]:
        logger.info(
            f"mdblist: {stats['absent']:,} titles marked absent — MDBList has "
            "no record of them, so they will be skipped until MDB_VERSION "
            "changes rather than re-requested every run"
        )
    if stats["no_local_row"] or stats["unmatched_payload"]:
        logger.warning(
            f"mdblist: {stats['unmatched_payload']:,} payloads carried no TMDB "
            f"id and {stats['no_local_row']:,} resolved to ids absent from "
            "media_metadata — these indicate a response-shape problem, unlike "
            "the 'absent' count above"
        )
    return stats


async def sync_lists(db, client: MDBListClient) -> dict:
    """Import membership for every active curated list."""
    stats = {"lists": 0, "items": 0}
    for row in db.query(MdbList).filter(MdbList.is_active.is_(True)).all():
        offset = 0
        imported = 0
        db.query(MdbListItem).filter(MdbListItem.list_slug == row.slug).delete(
            synchronize_session=False
        )
        while True:
            page = await client.list_items(row.slug, limit=1000, offset=offset)
            if not page:
                break
            items = page if isinstance(page, list) else page.get("items") or []
            if not items:
                break
            for rank, item in enumerate(items, start=offset + 1):
                tmdb_id = item.get("tmdb_id") or item.get("id")
                if not tmdb_id:
                    continue
                db.add(
                    MdbListItem(
                        list_slug=row.slug,
                        tmdb_id=int(tmdb_id),
                        media_type="tv" if item.get("mediatype") == "show" else "movie",
                        imdb_id=item.get("imdb_id"),
                        rank=rank,
                    )
                )
                imported += 1
            if len(items) < 1000:
                break
            offset += 1000
        row.item_count = imported
        row.synced_at = utc_now()
        db.commit()
        stats["lists"] += 1
        stats["items"] += imported
        logger.info(f"list {row.slug}: {imported:,} items")
    return stats


def audit_lists(db) -> list[dict]:
    """Score the tagger against human-curated list membership.

    This is the only ground truth available. A list whose members mostly lack
    the tag it implies is either a bad hint or a real blind spot in the tagger --
    and either is worth knowing, because nothing else in the system can tell you.
    """
    report = []
    for row in db.query(MdbList).filter(MdbList.is_active.is_(True)).all():
        for hint in row.tag_hints or []:
            tag = db.query(Tag).filter(Tag.name == hint).first()
            if tag is None:
                report.append({"list": row.slug, "tag": hint, "error": "unknown tag"})
                continue
            members = {
                (i.tmdb_id, i.media_type)
                for i in db.query(MdbListItem).filter(MdbListItem.list_slug == row.slug)
            }
            if not members:
                continue
            tagged = {
                (m.tmdb_id, m.media_type)
                for m in db.query(MovieTag).filter(MovieTag.tag_id == tag.id)
            }
            hits = len(members & tagged)
            report.append(
                {
                    "list": row.slug,
                    "tag": hint,
                    "members": len(members),
                    "with_tag": hits,
                    "precision": round(hits / len(members), 3),
                    "recall": round(hits / len(tagged), 3) if tagged else 0.0,
                }
            )
    return report


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ratings", action="store_true", help="Stage 1: bulk ratings")
    parser.add_argument(
        "--rating-lists",
        default="",
        # No default slugs. It used to ship 'top-rated-movies,top-rated-shows',
        # which were invented rather than looked up and 404 on every request --
        # the reason the admin card read "0 titles rated" while enrichment was
        # working. Ratings now arrive with the per-title facts (--enrich), so
        # this stage is only for real, operator-supplied list slugs.
        help="Comma-separated MDBList list slugs to pull ratings from. Must be "
        "slugs that exist; --enrich already stores ratings per title.",
    )
    parser.add_argument("--enrich", action="store_true", help="Stage 2: per-title")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--media-type", choices=("movie", "tv"), default=None)
    parser.add_argument("--add-list", default=None, metavar="SLUG")
    parser.add_argument("--tag-hint", default=None, help="Tag the list implies")
    parser.add_argument("--sync-lists", action="store_true")
    parser.add_argument("--audit-lists", action="store_true")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()

    with get_db() as db:
        if args.add_list:
            row = db.query(MdbList).filter(MdbList.slug == args.add_list).first()
            if row is None:
                row = MdbList(slug=args.add_list)
                db.add(row)
            row.tag_hints = [args.tag_hint] if args.tag_hint else []
            row.is_active = True
            db.commit()
            logger.info(f"list {args.add_list} registered (hint: {args.tag_hint})")
            return 0

        if args.audit_lists:
            for entry in audit_lists(db):
                logger.info(entry)
            return 0

        if args.status:
            total = db.query(func.count(MediaMetadata.tmdb_id)).scalar() or 0
            rated = (
                db.query(func.count(MediaMetadata.tmdb_id))
                .filter(MediaMetadata.rating_consensus.isnot(None))
                .scalar()
                or 0
            )
            enriched = (
                db.query(func.count(MediaMetadata.tmdb_id))
                .filter(MediaMetadata.mdb_enriched_at.isnot(None))
                .scalar()
                or 0
            )
            logger.info(f"library {total:,} titles")
            logger.info(f"  rating consensus: {rated:,} ({rated / max(1, total):.1%})")
            logger.info(
                f"  mdb facts:        {enriched:,} ({enriched / max(1, total):.1%})"
            )
            client = _client(db)
            if client:
                info = await client.user_info()
                logger.info(f"  key: {info if info else 'unreachable'}")
                logger.info(f"  quota remaining today: {client.remaining}")
                await client.close()
            return 0

        client = _client(db)
        if client is None:
            return 0
        try:
            info = await client.user_info()
            if info:
                logger.info(f"mdblist user: {info}")
            if args.ratings:
                slugs = [s.strip() for s in args.rating_lists.split(",") if s.strip()]
                if not slugs:
                    logger.warning(
                        "--ratings needs --rating-lists with slugs that exist. "
                        "Ratings for the whole library come from --enrich."
                    )
                else:
                    await sync_ratings(db, client, slugs)
            if args.sync_lists:
                await sync_lists(db, client)
            if args.enrich:
                await enrich_titles(db, client, args.limit, args.media_type)
        finally:
            await client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
