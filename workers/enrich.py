"""
Enrich the library with TMDB detail data, pruning payloads on the way in.

    docker compose --profile workers run --rm worker python workers/enrich.py
    python workers/enrich.py --limit 5000            # partial pass
    python workers/enrich.py --media-type tv
    python workers/enrich.py --compact-only          # prune existing rows, no fetching
    python workers/enrich.py --collections-only      # backfill franchise columns, no fetching

The build fetches titles from TMDB's *list* endpoints, which carry no keywords,
credits, runtime or certification. The tagger has therefore only ever seen a
title, its genres and a truncated overview. TMDB keywords in particular are the
single highest-signal field available and were being discarded entirely.

Two things make this cheap:

**Reuse before fetching.** ``get_movie``/``get_tv_show`` already append
``credits,keywords,external_ids,images,videos,release_dates``, and anything that
went through them left a full payload in ``raw_data``. Those rows need no
network call at all.

**Prune before storing.** A TMDB detail payload runs 50-250KB, dominated by
``images``. Yet ``raw_data`` is read in exactly one place in the entire
repository -- ``workers/fingerprint_worker.py``, for ``seasons[].episode_count``.
Everything else defers the column because it is known to be too big to touch.
Storing detail payloads unpruned across the library would add roughly 17GB to
serve one integer field. Pruning takes a payload to ~5KB.

Ordered by popularity descending, so an interrupted run leaves the part of the
library that actually appears in catalogs done.
"""

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any, Callable, Iterable

import httpx
from loguru import logger
from sqlalchemy import func, tuple_

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.time_utils import utc_now  # noqa: E402
from app.config import settings  # noqa: E402
from app.database import get_db  # noqa: E402
from app.models import MediaMetadata  # noqa: E402
from app.rate_limit import RateLimiter  # noqa: E402
from app.tmdb_client import tmdb_client  # noqa: E402
from workers.build_control import stop_event  # noqa: E402
from workers.metadata_store import upsert_media_metadata  # noqa: E402

# 1.2.0 reruns the canonical identity/date repair after metadata writes learned
# to distinguish detail-owned fields from discovery-list fields. Existing local
# detail payloads are reused, so most upgraded rows require no TMDB request.
ENRICH_VERSION = "1.2.0"

#: Stamped on titles TMDB returns 404 for. Distinct from ENRICH_VERSION so the
#: row is excluded from both the "needs enriching" and the "is enriched" sets:
#: it will never succeed, and counting it as pending forever is what made
#: "remaining" misleading across runs. ``--force`` still re-attempts it.
MISSING_VERSION = "1.0.0-missing"

#: Crew jobs worth keeping. Everything else in a credits block is noise for
#: tagging and bulk for storage.
_KEEP_JOBS = {"Director", "Writer", "Screenplay", "Creator", "Novel", "Story"}


def prune_raw_data(payload: dict) -> dict:
    """Strip a TMDB detail payload down to what is actually read back.

    Drops ``images`` and ``videos`` outright, trims ``credits`` to the top 20
    billed cast plus key crew, keeps ``release_dates``/``content_ratings`` for
    US and GB only, and reduces ``seasons`` to the three fields the fingerprint
    worker needs. Idempotent: an already-pruned payload passes through unchanged.
    """
    if not isinstance(payload, dict):
        return {}

    pruned = {
        key: value
        for key, value in payload.items()
        if key
        not in ("images", "videos", "credits", "release_dates", "content_ratings")
    }

    credits = payload.get("credits")
    if isinstance(credits, dict):
        cast = [
            {
                "name": person.get("name"),
                "character": person.get("character"),
                "order": person.get("order"),
            }
            for person in (credits.get("cast") or [])[:20]
            if isinstance(person, dict) and person.get("name")
        ]
        crew = [
            {"name": person.get("name"), "job": person.get("job")}
            for person in (credits.get("crew") or [])
            if isinstance(person, dict) and person.get("job") in _KEEP_JOBS
        ][:12]
        if cast or crew:
            pruned["credits"] = {"cast": cast, "crew": crew}

    for key in ("release_dates", "content_ratings"):
        block = payload.get(key)
        if isinstance(block, dict) and isinstance(block.get("results"), list):
            kept = [
                entry
                for entry in block["results"]
                if isinstance(entry, dict) and entry.get("iso_3166_1") in ("US", "GB")
            ]
            pruned[key] = {"results": kept}

    seasons = payload.get("seasons")
    if isinstance(seasons, list):
        # workers/fingerprint_worker.py reads seasons[].episode_count. Keep only
        # that plus enough to identify the season.
        pruned["seasons"] = [
            {
                "season_number": season.get("season_number"),
                "episode_count": season.get("episode_count"),
                "air_date": season.get("air_date"),
            }
            for season in seasons
            if isinstance(season, dict)
        ]

    return pruned


def _certification(payload: dict, media_type: str) -> str | None:
    """US certification from a detail payload, if present."""
    key = "release_dates" if media_type == "movie" else "content_ratings"
    block = payload.get(key)
    if not isinstance(block, dict):
        return None
    for entry in block.get("results") or []:
        if not isinstance(entry, dict) or entry.get("iso_3166_1") != "US":
            continue
        if media_type == "tv":
            rating = entry.get("rating")
            return str(rating) if rating else None
        for release in entry.get("release_dates") or []:
            cert = (release or {}).get("certification")
            if cert:
                return str(cert)
    return None


def build_tag_facts(payload: dict, media_type: str) -> dict:
    """Distil a detail payload into the fields the tagging prompt uses.

    Kept separate from ``raw_data`` so the prompt builder reads a small, stable
    shape rather than parsing a TMDB payload on every batch.
    """
    facts: dict[str, Any] = {}
    credits = payload.get("credits") or {}

    directors = [
        person.get("name")
        for person in (credits.get("crew") or [])
        if isinstance(person, dict) and person.get("job") in ("Director", "Creator")
    ]
    if not directors:
        directors = [
            person.get("name")
            for person in (payload.get("created_by") or [])
            if isinstance(person, dict) and person.get("name")
        ]
    if directors:
        facts["directors"] = [d for d in directors if d][:3]

    cast = [
        person.get("name")
        for person in (credits.get("cast") or [])[:6]
        if isinstance(person, dict) and person.get("name")
    ]
    if cast:
        facts["cast"] = cast

    keywords_block = payload.get("keywords") or {}
    raw_keywords = (
        (keywords_block.get("keywords") if isinstance(keywords_block, dict) else None)
        or (keywords_block.get("results") if isinstance(keywords_block, dict) else None)
        or []
    )
    keywords = [
        str(k.get("name"))
        for k in raw_keywords
        if isinstance(k, dict) and k.get("name")
    ][:12]
    if keywords:
        facts["keywords"] = keywords

    runtime = payload.get("runtime")
    if not runtime:
        episode_runtimes = payload.get("episode_run_time") or []
        if episode_runtimes:
            runtime = sum(episode_runtimes) / len(episode_runtimes)
    if runtime:
        facts["runtime"] = int(runtime)

    cert = _certification(payload, media_type)
    if cert:
        facts["certification"] = cert

    countries = [
        c.get("iso_3166_1")
        for c in (payload.get("production_countries") or [])
        if isinstance(c, dict) and c.get("iso_3166_1")
    ]
    if countries:
        facts["countries"] = countries[:4]

    companies = [
        c.get("name")
        for c in (payload.get("production_companies") or [])
        if isinstance(c, dict) and c.get("name")
    ]
    if companies:
        facts["studios"] = companies[:2]

    networks = [
        n.get("name")
        for n in (payload.get("networks") or [])
        if isinstance(n, dict) and n.get("name")
    ]
    if networks:
        facts["networks"] = networks[:2]

    collection = payload.get("belongs_to_collection")
    if isinstance(collection, dict) and collection.get("name"):
        facts["collection"] = collection["name"]

    if payload.get("status"):
        facts["status"] = payload["status"]

    return facts


def collection_fields(payload: dict) -> tuple[int | None, str | None]:
    """``(collection_id, collection_name)`` from a TMDB detail payload.

    ``belongs_to_collection`` survives ``prune_raw_data`` (only images, videos,
    credits and the certification blocks are dropped), so every already-enriched
    movie can be backfilled without a TMDB request. TV payloads never carry it.
    """
    collection = payload.get("belongs_to_collection")
    if not isinstance(collection, dict):
        return None, None
    raw_id = collection.get("id")
    name = collection.get("name")
    if not isinstance(raw_id, int) or not name:
        return None, None
    return raw_id, str(name)[:200]


async def backfill_collections(limit: int | None = None) -> dict:
    """Promote ``belongs_to_collection`` out of ``raw_data`` into its columns.

    Network-free: it reads payloads already stored. Search groups a franchise
    into one release-ordered block using these columns, and simply does not
    group rows where they are still NULL -- so this is a quality upgrade that
    can run at any time, not a prerequisite for search to work.

    Batched and idempotent: only rows that still have no ``collection_id`` are
    considered, so an interrupted run resumes where it stopped.
    """
    batch_size = 2_000
    scanned = 0
    filled = 0
    # Paginate on the primary key, not on ``collection_id IS NULL``: a movie TMDB
    # places in no collection stays NULL, so a predicate-only pager would re-read
    # the same unfillable rows forever and never reach the tail of the table.
    cursor = -1
    while True:
        with get_db() as db:
            rows = (
                db.query(
                    MediaMetadata.tmdb_id,
                    MediaMetadata.raw_data,
                )
                .filter(
                    MediaMetadata.media_type == "movie",
                    MediaMetadata.collection_id.is_(None),
                    MediaMetadata.raw_data.isnot(None),
                    MediaMetadata.tmdb_id > cursor,
                )
                .order_by(MediaMetadata.tmdb_id.asc())
                .limit(batch_size)
                .all()
            )
            if not rows:
                break
            cursor = rows[-1][0]
            updates = []
            for tmdb_id, payload in rows:
                if not isinstance(payload, dict):
                    continue
                collection_id, collection_name = collection_fields(payload)
                if collection_id is not None:
                    updates.append(
                        {
                            "b_tmdb_id": tmdb_id,
                            "collection_id": collection_id,
                            "collection_name": collection_name,
                        }
                    )
            for update in updates:
                db.query(MediaMetadata).filter(
                    MediaMetadata.tmdb_id == update["b_tmdb_id"],
                    MediaMetadata.media_type == "movie",
                ).update(
                    {
                        MediaMetadata.collection_id: update["collection_id"],
                        MediaMetadata.collection_name: update["collection_name"],
                    },
                    synchronize_session=False,
                )
            filled += len(updates)
            db.commit()
            scanned += len(rows)

        logger.info(f"collections: scanned {scanned:,}, filled {filled:,}")
        if len(rows) < batch_size or (limit and scanned >= limit):
            break

    logger.info(f"collections: {filled:,} of {scanned:,} rows carry a collection")
    return {"scanned": scanned, "filled": filled}


async def compact_existing(limit: int | None = None) -> dict:
    """Prune payloads already stored, reporting LOGICAL bytes reclaimed.

    Physical table size will not fall from this alone: Postgres marks the old
    tuples reusable inside the table rather than returning extents to the OS,
    and the table transiently grows while both versions coexist. Reclaiming disk
    needs ``VACUUM FULL media_metadata`` (ACCESS EXCLUSIVE, minutes of outage)
    or ``pg_repack``. Reporting logical bytes keeps "the pruning worked" separate
    from "the disk came back", which are otherwise indistinguishable.
    """
    import json

    before = after = 0
    touched = 0
    with get_db() as db:
        query = db.query(MediaMetadata).filter(MediaMetadata.raw_data.isnot(None))
        if limit:
            query = query.limit(limit)
        for row in query.all():
            payload = row.raw_data
            if not isinstance(payload, dict):
                continue
            original = len(json.dumps(payload))
            pruned = prune_raw_data(payload)
            new_size = len(json.dumps(pruned))
            if new_size < original:
                row.raw_data = pruned
                touched += 1
            before += original
            after += min(new_size, original)
        db.commit()

    logger.info(
        f"compact: {touched:,} rows pruned, logical {before / 1e6:,.0f}MB -> "
        f"{after / 1e6:,.0f}MB ({(1 - after / before) * 100 if before else 0:.0f}% smaller)"
    )
    logger.info(
        "Physical size will not drop until VACUUM FULL media_metadata (or pg_repack)."
    )
    return {"rows": touched, "logical_before": before, "logical_after": after}


async def enrich(
    media_type: str | None = None,
    limit: int | None = None,
    force: bool = False,
    on_progress: "Callable[[int, int], None] | None" = None,
    tmdb_ids: "Iterable[int] | None" = None,
) -> dict:
    """Fetch TMDB detail for titles that lack it, pruning and distilling as we go.

    Args:
        on_progress: Called ``(done, total)`` after each committed chunk. The
            upgrade UI renders it into the step card -- without it this stage
            shows a bare spinner for hours, which is indistinguishable from
            being stuck.
        tmdb_ids: Restrict to these ids. The nightly job uses it to enrich the
            handful of titles that just landed on a shelf, rather than walking
            the whole library by popularity -- a title in tonight's Trending
            row can sit far down a 316k-row popularity walk.
    """
    stats = {"reused": 0, "fetched": 0, "failed": 0, "missing": 0, "stopped": False}
    limiter = RateLimiter(settings.tmdb_enrich_rps)
    semaphore = asyncio.Semaphore(settings.tmdb_enrich_concurrency)
    measured: list[int] = []

    with get_db() as db:
        query = db.query(MediaMetadata.tmdb_id, MediaMetadata.media_type).order_by(
            MediaMetadata.popularity.desc().nullslast()
        )
        if media_type:
            query = query.filter(MediaMetadata.media_type == media_type)
        if tmdb_ids is not None:
            scoped = list(tmdb_ids)
            if not scoped:
                return stats
            query = query.filter(MediaMetadata.tmdb_id.in_(scoped))
        if not force:
            query = query.filter(
                (MediaMetadata.enrich_version.is_(None))
                | (
                    MediaMetadata.enrich_version.notin_(
                        (ENRICH_VERSION, MISSING_VERSION)
                    )
                )
            )
        if limit:
            query = query.limit(limit)
        targets = [(int(r[0]), r[1]) for r in query.all()]

    logger.info(f"enrich: {len(targets):,} titles to process")
    if not targets:
        return stats

    async def one(
        tmdb_id: int, mtype: str, existing: dict | None
    ) -> tuple[int, str, dict | None, bool | str]:
        """Returns (tmdb_id, media_type, payload, reused)."""
        # Reuse: a payload carrying credits came from a detail call already.
        if isinstance(existing, dict) and "credits" in existing:
            return tmdb_id, mtype, existing, True

        async with semaphore:
            await limiter.wait()
            try:
                if mtype == "movie":
                    payload = await tmdb_client.get_movie(tmdb_id)
                else:
                    payload = await tmdb_client.get_tv_show(tmdb_id)
                return tmdb_id, mtype, payload, False
            except httpx.HTTPStatusError as exc:
                # 404 means the id is gone from TMDB, which never resolves on a
                # retry. Left unmarked, every dead id in the library is
                # re-fetched on every future run -- permanently inflating
                # "remaining" and burning requests. Mark it so the resume query
                # skips it; transient failures below stay unmarked and retry.
                if exc.response.status_code == 404:
                    logger.debug(f"enrich {mtype}/{tmdb_id}: gone from TMDB (404)")
                    return tmdb_id, mtype, None, "missing"
                logger.debug(f"enrich {mtype}/{tmdb_id}: {exc}")
                return tmdb_id, mtype, None, False
            except Exception as exc:  # noqa: BLE001 - one title must not stop the pass
                logger.debug(f"enrich {mtype}/{tmdb_id}: {exc}")
                return tmdb_id, mtype, None, False

    batch_size = settings.enrich_batch_size
    for start in range(0, len(targets), batch_size):
        if stop_event.is_set():
            # Without this the Stop button was a lie during the longest step in
            # the pipeline: the flag was only read between steps, so stopping
            # during a ~5h enrichment did nothing until it finished on its own.
            # Break rather than raise, so the committed work is reported.
            logger.warning(
                f"enrich: stop requested at {start:,}/{len(targets):,} — "
                "committed work is kept and the next run resumes from here"
            )
            stats["stopped"] = True
            break
        chunk = targets[start : start + batch_size]
        with get_db() as db:
            existing_payloads = {
                (int(tmdb_id), str(mtype)): payload
                for tmdb_id, mtype, payload in db.query(
                    MediaMetadata.tmdb_id,
                    MediaMetadata.media_type,
                    MediaMetadata.raw_data,
                )
                .filter(
                    tuple_(MediaMetadata.tmdb_id, MediaMetadata.media_type).in_(chunk)
                )
                .all()
            }
        results = await asyncio.gather(
            *(one(t, m, existing_payloads.get((t, m))) for t, m in chunk)
        )

        with get_db() as db:
            result_keys = [(tmdb_id, mtype) for tmdb_id, mtype, _, _ in results]
            rows = {
                (int(row.tmdb_id), str(row.media_type)): row
                for row in db.query(MediaMetadata)
                .filter(
                    tuple_(MediaMetadata.tmdb_id, MediaMetadata.media_type).in_(
                        result_keys
                    )
                )
                .all()
            }
            for tmdb_id, mtype, payload, reused in results:
                row = rows.get((tmdb_id, mtype))
                if payload is None:
                    stats["failed"] += 1
                    if reused == "missing" and row is not None:
                        row.enrich_version = MISSING_VERSION
                        stats["missing"] += 1
                    continue
                if row is None:
                    continue

                import json

                if len(measured) < 1000:
                    measured.append(len(json.dumps(payload)))

                # Reuse the detail payload to repair canonical metadata before
                # pruning it. This is a local database operation for the
                # hundreds of thousands of rows that already hold detail data;
                # it does not add a TMDB request or any AI spend.
                repaired = tmdb_client.extract_metadata(payload, mtype)
                # ``extract_metadata`` uses display-friendly empty fallbacks for
                # malformed/list payloads. They must not erase a known value
                # during a repair pass whose source unexpectedly lacks a field.
                title_key = "title" if mtype == "movie" else "name"
                date_key = "release_date" if mtype == "movie" else "first_air_date"
                if not payload.get(title_key):
                    repaired["title"] = None
                if not payload.get(date_key):
                    repaired["release_date"] = None
                if "overview" not in payload:
                    repaired["overview"] = None
                if "genres" not in payload and "genre_ids" not in payload:
                    repaired["genres"] = None
                upsert_media_metadata(db, repaired)
                row.tag_facts = build_tag_facts(payload, mtype)
                # Franchise columns for search grouping. Only ever set from a
                # payload that names a collection, so a TV row or a standalone
                # film keeps its NULLs rather than being stamped with a blank.
                collection_id, collection_name = collection_fields(payload)
                if collection_id is not None:
                    row.collection_id = collection_id
                    row.collection_name = collection_name
                row.raw_data = (
                    prune_raw_data(payload) if settings.raw_data_prune else payload
                )
                if not row.imdb_id:
                    external = payload.get("external_ids") or {}
                    if external.get("imdb_id"):
                        row.imdb_id = external["imdb_id"]
                row.enriched_at = utc_now()
                row.enrich_version = ENRICH_VERSION
                stats["reused" if reused else "fetched"] += 1
            db.commit()

        done = min(start + batch_size, len(targets))
        logger.info(f"enrich: {done:,}/{len(targets):,}")
        if on_progress:
            try:
                on_progress(done, len(targets))
            except Exception:  # noqa: BLE001 - progress must never fail the run
                pass

        # Confirm the storage projection at ~1,000 rows rather than ~350,000.
        if measured and len(measured) >= 1000 and start < batch_size * 3:
            mean = sum(measured) / len(measured)
            logger.info(
                f"enrich: measured mean payload {mean / 1024:.1f}KB over "
                f"{len(measured)} rows (projection assumed ~80KB unpruned, "
                f"~5KB pruned)"
            )
            measured.append(0)  # log once

    logger.info(
        f"enrich complete: {stats['fetched']:,} fetched, {stats['reused']:,} reused "
        f"(no network), {stats['failed']:,} failed"
    )
    return stats


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--media-type", choices=("movie", "tv"), default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--force", action="store_true", help="Re-enrich even at the current version"
    )
    parser.add_argument(
        "--compact-only",
        action="store_true",
        help="Prune payloads already stored and exit, without fetching",
    )
    parser.add_argument(
        "--collections-only",
        action="store_true",
        help="Backfill franchise columns from stored payloads and exit, without fetching",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.compact_only:
        await compact_existing(args.limit)
        return 0

    if args.collections_only:
        await backfill_collections(args.limit)
        return 0

    if args.dry_run:
        with get_db() as db:
            total = db.query(func.count(MediaMetadata.tmdb_id)).scalar() or 0
            pending = (
                db.query(func.count(MediaMetadata.tmdb_id))
                .filter(
                    (MediaMetadata.enrich_version.is_(None))
                    | (MediaMetadata.enrich_version != ENRICH_VERSION)
                )
                .scalar()
                or 0
            )
            reusable = (
                db.query(func.count(MediaMetadata.tmdb_id))
                .filter(MediaMetadata.raw_data.isnot(None))
                .scalar()
                or 0
            )
        logger.info(f"library {total:,} titles; {pending:,} need enrichment")
        logger.info(f"{reusable:,} have a stored payload that may avoid a fetch")
        hours = pending / max(1.0, settings.tmdb_enrich_rps) / 3600
        logger.info(f"at {settings.tmdb_enrich_rps} req/s that is ~{hours:.1f} hours")
        return 0

    await enrich(args.media_type, args.limit, args.force)
    await tmdb_client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
