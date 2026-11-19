"""
The whole tagging upgrade as one resumable pipeline, driven from the admin UI.

Everything this runs already existed as a separate worker script with its own
flags. Chaining them by hand meant six ``docker compose run`` invocations in a
dependency order nobody should have to remember, so this is the button:

    (enrich || mdblist) -> rebuild tags -> relevance -> catalogs -> user rows

**Every step is individually resumable and individually skippable**, which is
what makes one button safe. Each one selects its own work with a "what is not
done yet" query -- ``enrich_version``, ``mdb_version``, ``tagger_version`` --
so an interrupted run picks up where it stopped rather than starting over or
double-charging. Pressing the button twice is not destructive; the second press
finds most of the work already done and skips it.

Order is a real dependency chain, not a preference. Both fetch stages feed the
tagging prompt -- TMDB supplies keywords, cast and certification, MDBList
supplies review prose -- so **the tagger waits for both**. It is the one place
where overlapping stages actively costs money rather than saving time: a title
tagged before its reviews arrive is `stale_facts` by construction and has to be
re-tagged later at roughly twice the prompt size, so racing ahead buys the same
work twice. Relevance is computed from the tags and catalogs from the relevance
weights, so those follow in turn. Running any of it out of order silently
produces a worse result rather than an error, which is exactly why the order is
encoded here instead of documented.

Only the last two steps are visible to users: catalogs are served from
pre-computed snapshots, so rows keep rendering the old contents for the whole
run and change once, at the end. The per-user rows are a snapshot too, and they
are rebuilt from the same tags -- which is why ``users`` is part of the pipeline
rather than left to the nightly scheduler. Without it the run reports "done"
while every "Because You Watched" row is still built from the old tags.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from loguru import logger

from app.time_utils import utc_now  # noqa: E402
from app.config import settings
from app.database import get_db
from app.models import MediaMetadata, UniversalCatalogContent, UniversalCategory
from sqlalchemy import func
from workers.build_control import (
    BuildStopped,
    raise_if_stopped,
    reset_build_stop,
    stop_event,
)

#: Ordered pipeline. ``key`` is what the UI renders and what ``skip`` matches.
STEPS: tuple[dict[str, Any], ...] = (
    {
        "key": "enrich",
        "title": "Fetch TMDB details",
        "detail": "Keywords, cast, runtime and certification for the tagging prompt.",
        "needs": (),
        "optional": False,
    },
    {
        "key": "mdblist",
        "title": "Fetch MDBList ratings and facts",
        "detail": "Multi-source ratings plus review text. Skipped without a key.",
        "needs": ("mdblist",),
        "optional": True,
    },
    {
        "key": "retag",
        "title": "Re-tag the library",
        "detail": "The only step that costs money.",
        "needs": ("tagging",),
        "optional": False,
    },
    {
        "key": "relevance",
        "title": "Compute tag relevance",
        "detail": "IDF weights and document vectors. No API calls.",
        "needs": (),
        "optional": False,
    },
    {
        "key": "catalogs",
        "title": "Regenerate universal catalogs",
        "detail": "Synthesized discovery rows every profile shares.",
        "needs": (),
        "optional": False,
    },
    {
        "key": "users",
        "title": "Rebuild personalized rows",
        "detail": "Because You Watched, Top Picks, taste and hidden-gem rows.",
        "needs": (),
        "optional": False,
    },
)

STEP_KEYS = tuple(s["key"] for s in STEPS)
PUBLICATION_STEPS = ("relevance", "catalogs", "users")


def catalog_publication_coverage(db) -> dict[str, int | bool]:
    """Return whether the current tag version is complete enough to publish.

    Synthesis deliberately reads only current-version tags.  Publishing while
    ``_pending_query`` is non-empty therefore replaces the complete catalog
    snapshot with rows derived from a pilot-sized fragment of the library.
    The pending query already treats terminal model exclusions as finished, so
    an exact zero is both reachable and the only unambiguous publication gate.
    """
    from app.catalog_generator import quality_floor_clauses
    from app.models import MovieTag
    from workers.retag import _pending_query
    from workers.tagging_common import tagger_version

    eligible_query = db.query(func.count(MediaMetadata.tmdb_id))
    quality_clauses = quality_floor_clauses()
    if quality_clauses:
        eligible_query = eligible_query.filter(*quality_clauses)
    eligible = int(eligible_query.scalar() or 0)
    pending = int(_pending_query(db).count())

    current_query = (
        db.query(MovieTag.tmdb_id, MovieTag.media_type)
        .join(
            MediaMetadata,
            (MediaMetadata.tmdb_id == MovieTag.tmdb_id)
            & (MediaMetadata.media_type == MovieTag.media_type),
        )
        .filter(MovieTag.tagger_version == tagger_version())
    )
    if quality_clauses:
        current_query = current_query.filter(*quality_clauses)
    current = int(current_query.distinct().count())
    return {
        "ready": pending == 0,
        "eligible": eligible,
        "current": current,
        "pending": pending,
    }


def require_catalog_publication_ready(db) -> dict[str, int | bool]:
    """Refuse to replace discovery rows with a partial tag-version snapshot."""
    coverage = catalog_publication_coverage(db)
    if not coverage["ready"]:
        raise RuntimeError(
            "Catalog publication blocked: "
            f"{coverage['pending']:,} of {coverage['eligible']:,} quality-eligible "
            "titles still need the current tag version. Finish tagging first; "
            "the existing catalog snapshot was left unchanged."
        )
    return coverage


def preflight() -> dict[str, Any]:
    """What the upgrade needs, what it has, and what each step would do.

    Called before anything runs so the UI can ask for a missing key instead of
    failing four steps in.
    """
    from app.mdblist_client import get_api_key
    from app.tagging_provider import (
        active_model,
        normalize_provider,
        provider_label,
        provider_options,
        provider_ready,
        production_model_ready,
    )
    from workers.enrich import ENRICH_VERSION, MISSING_VERSION
    from workers.mdb_sync import MDB_ABSENT_VERSION, MDB_VERSION
    from workers.retag import (
        EST_INPUT_TOKENS,
        EST_OUTPUT_TOKENS,
        _pending_query,
        estimated_ai_titles,
    )
    from workers.tagging_common import estimate_cost, tagger_version

    with get_db() as db:
        total = db.query(func.count(MediaMetadata.tmdb_id)).scalar() or 0
        # Both counters must exclude their "done, permanently" sentinels or the
        # card shows work that will never be attempted again and "remaining"
        # never reaches zero:
        #
        #   enrich  MISSING_VERSION      -- the id is gone from TMDB (404)
        #   mdblist MDB_ABSENT_VERSION   -- MDBList has no record of the title
        #
        # In production that is ~27,776 titles for MDBList alone, so the card
        # would sit at "27,776 remaining" through every future run while the
        # step correctly skipped all of them. These are the exact "remaining is
        # misleading across runs" failures the sentinels were added to remove.
        need_enrich = (
            db.query(func.count(MediaMetadata.tmdb_id))
            .filter(
                (MediaMetadata.enrich_version.is_(None))
                | (
                    MediaMetadata.enrich_version.notin_(
                        (ENRICH_VERSION, MISSING_VERSION)
                    )
                )
            )
            .scalar()
            or 0
        )
        need_mdb = (
            db.query(func.count(MediaMetadata.tmdb_id))
            .filter(
                (MediaMetadata.mdb_version.is_(None))
                | (MediaMetadata.mdb_version.notin_((MDB_VERSION, MDB_ABSENT_VERSION)))
            )
            .scalar()
            or 0
        )
        # Same strict corpus as the actual run. The old preflight priced every
        # acquired title while run-time selection evolved separately, which
        # made the estimate both alarming and untrustworthy.
        need_retag = _pending_query(db).count()
        from app.catalog_generator import quality_floor_clauses

        eligible_query = db.query(func.count(MediaMetadata.tmdb_id))
        quality_clauses = quality_floor_clauses()
        if quality_clauses:
            eligible_query = eligible_query.filter(*quality_clauses)
        eligible_titles = eligible_query.scalar() or 0
        generated_rows = (
            db.query(func.count(UniversalCategory.id))
            .filter(
                UniversalCategory.source == "generated",
                UniversalCategory.is_active.is_(True),
            )
            .scalar()
            or 0
        )
        has_mdblist = bool(get_api_key(db))

    provider = normalize_provider()
    keys = {
        "tmdb": bool(settings.tmdb_api_key),
        "gemini": bool(settings.gemini_api_key),
        "anthropic": bool(settings.anthropic_api_key),
        "openai": bool(settings.openai_api_key),
        "tagging": provider_ready(provider),
        "mdblist": has_mdblist,
    }
    ai_retag = estimated_ai_titles(need_retag)
    cost = estimate_cost(ai_retag * EST_INPUT_TOKENS, ai_retag * EST_OUTPUT_TOKENS)

    remaining = {
        "enrich": need_enrich,
        "mdblist": need_mdb if has_mdblist else 0,
        "retag": need_retag,
        # These are logical stages rather than title queues. Show one unit when
        # upstream work makes their snapshots stale, or when synthesis has
        # never produced a row, so Admin Center does not misleadingly say
        # "nothing to do" beside steps that are required for publication.
        "relevance": 1 if need_retag else 0,
        "catalogs": 1 if need_retag or not generated_rows else 0,
        "users": 1 if need_retag else 0,
    }
    return {
        "library_total": total,
        "eligible_titles": eligible_titles,
        "excluded_by_quality": max(0, total - eligible_titles),
        "keys": keys,
        # TMDB gates enrichment; the selected AI provider gates tagging.
        "ready": keys["tmdb"] and keys["tagging"],
        "missing_required": [
            key
            for key, available in (
                ("tmdb", keys["tmdb"]),
                (f"{provider}_api_key", keys["tagging"]),
            )
            if not available
        ],
        "remaining": remaining,
        "est_cost_usd": round(cost, 2),
        "estimated_ai_titles": ai_retag,
        "estimated_local_only": need_retag - ai_retag,
        "cost_basis": "taxonomy-v5-offline-replay-conservative",
        "cost_ceiling_usd": settings.retag_cost_ceiling_usd,
        "provider": provider,
        "provider_label": provider_label(provider),
        "providers": provider_options(),
        "model": active_model(provider),
        "model_production_ready": production_model_ready(provider),
        "tagger_version": tagger_version(),
        "quality_gate": {
            "min_quality": settings.catalog_min_quality,
            "min_vote_count": settings.catalog_min_vote_count,
            "min_rating_sources": settings.catalog_min_rating_sources,
            "niche_min_quality": settings.catalog_niche_min_quality,
            "niche_min_vote_count": settings.catalog_niche_min_vote_count,
        },
        "steps": [
            {
                **{k: v for k, v in step.items() if k != "needs"},
                "available": all(keys.get(n, False) for n in step["needs"]),
                "remaining": remaining.get(step["key"], 0),
            }
            for step in STEPS
        ],
    }


async def run_upgrade(
    *,
    skip: tuple[str, ...] = (),
    limit: int | None = None,
    min_votes: int = 0,
    on_step: Callable[[dict], None] | None = None,
) -> dict[str, Any]:
    """Run the pipeline. Returns a per-step report.

    Args:
        skip: Step keys to leave out.
        limit: Cap on titles for the enrich and retag steps. Limited runs are
            validation pilots and can never publish relevance, catalogs, or
            user rows.
        min_votes: Skip titles below this TMDB vote count when re-tagging.
            This is an optional additional scope on top of the shared catalog
            quality gate.
        on_step: Called with a status dict as each step starts and finishes.

    Source enrichment failures do not erase the currently served library, but
    a failed re-tag/relevance/catalog stage blocks every downstream publication
    stage. Publishing a partial target version made a safety halt look like a
    successful upgrade and encouraged another paid run.
    """
    from app.mdblist_client import MDBListClient, get_api_key

    reset_build_stop()
    effective_skip = set(skip)
    pilot_skipped: set[str] = set()
    if limit is not None:
        # A pilot proves provider behaviour and cost. It is not a miniature
        # production release: synthesis reads only current-version tags, so a
        # 1,000-title pilot used to deactivate a complete catalog snapshot and
        # replace it with whatever three rows that fragment happened to form.
        pilot_skipped = set(PUBLICATION_STEPS) - effective_skip
        effective_skip.update(PUBLICATION_STEPS)
    started = utc_now()
    report: dict[str, Any] = {
        "started_at": started.isoformat(),
        "steps": [],
        "stopped": False,
        "publication_blocked": False,
    }

    def emit(entry: dict) -> None:
        if on_step:
            on_step({"current": entry, "steps": report["steps"]})

    # Entries are created up front in STEPS order so the UI list stays stable
    # even though the first three now finish out of order.
    entries: dict[str, dict[str, Any]] = {}
    for step in STEPS:
        key = str(step["key"])
        entry = {
            "key": key,
            "title": step["title"],
            "status": "skipped" if key in effective_skip else "pending",
            "detail": (
                "Pilot safety — publication waits for the completion run"
                if key in pilot_skipped
                else "Skipped by request" if key in effective_skip else ""
            ),
        }
        entries[key] = entry
        report["steps"].append(entry)
    emit(report["steps"][0])

    #: enrich and mdblist hit different hosts and neither is CPU-bound, so they
    #: overlap. The re-tag does NOT join them.
    #:
    #: It used to. Both source stages and the tagger ran together, and because
    #: the tagger only waited on TMDB enrichment it raced ahead of MDBList --
    #: tagging titles whose reviews had not arrived yet. Every such title is by
    #: definition `stale_facts` and has to be re-tagged later at a ~2x prompt,
    #: so the concurrency did not save work, it bought the same work twice. The
    #: first full run ended with 98,270 titles tagged and only 8,343 holding
    #: MDBList facts: ~91,000 pre-bought re-tags.
    #:
    #: Gating the tagger on both sources costs the difference between the two
    #: fetch stages -- they finish within an hour of each other -- and makes
    #: every title cost exactly one tagging call. relevance reads the tags and
    #: catalogs read the weights, so those follow as they always did.
    CONCURRENT = ("enrich", "mdblist")
    SEQUENTIAL = ("retag", "relevance", "catalogs", "users")

    #: Set once BOTH source stages are done, so the re-tag takes its immutable
    #: work snapshot only after every available fact has landed.
    enrich_done = asyncio.Event()

    async def run_step(key: str) -> None:
        entry = entries[key]
        if entry["status"] == "skipped":
            return
        entry["status"] = "running"
        entry["started_at"] = utc_now().isoformat()
        emit(entry)
        logger.info(f"upgrade: {entry['title']}")

        try:
            raise_if_stopped()

            if key == "enrich":
                from workers.enrich import enrich

                def _enrich_progress(done: int, total: int) -> None:
                    # Written straight into the step card the 4s poll reads.
                    # Without it this stage renders an empty spinner for hours,
                    # which is indistinguishable from being wedged.
                    entry["detail"] = f"{done:,} / {total:,}"
                    emit(entry)

                stats = await enrich(limit=limit, on_progress=_enrich_progress)
                entry["detail"] = (
                    f"{stats['fetched']:,} fetched, {stats['reused']:,} reused, "
                    f"{stats['failed']:,} failed"
                    + (
                        f", {stats['missing']:,} gone from TMDB"
                        if stats.get("missing")
                        else ""
                    )
                )
                entry["stats"] = stats

            elif key == "mdblist":
                with get_db() as db:
                    api_key = get_api_key(db)
                if not api_key:
                    entry.update(
                        status="skipped", detail="No MDBList key — using TMDB ratings"
                    )
                    emit(entry)
                    return
                from workers.mdb_sync import enrich_titles

                def _mdb_progress(enriched: int, attempted: int, total: int) -> None:
                    # Reports ENRICHED, not attempted, because the two diverged
                    # badly once: a run attempted 34,000 titles and stored 246,
                    # and the progress line -- which counted ids requested --
                    # advanced smoothly through all of it.
                    entry["detail"] = (
                        f"{enriched:,} enriched / {attempted:,} attempted "
                        f"of {total:,}"
                    )
                    emit(entry)

                with get_db() as db:
                    client = MDBListClient(api_key, db)
                    client.load_quota()
                    # Not optional. This is the ONLY place the daily ceiling and
                    # the batch cap are discovered -- MDBList sends no
                    # X-RateLimit-* headers, so without it the quota reserve is
                    # inert and every batch is capped at the conservative 10
                    # rather than the 100 a supporter key allows (a tenfold
                    # difference in requests for identical work).
                    await client.user_info()
                    try:
                        # No separate stage-1 ratings pass. It used to call
                        # sync_ratings with two invented list slugs
                        # ('top-rated-movies', 'top-rated-shows') that 404 on
                        # every request, which is why the card reported "0
                        # titles rated" while enrichment was working fine. The
                        # per-title batch endpoint returns ratings in the same
                        # payload as the facts, so stage 1 was redundant as well
                        # as broken -- enrich_titles stores them as it goes.
                        facts = await enrich_titles(
                            db, client, limit, None, on_progress=_mdb_progress
                        )
                    finally:
                        await client.close()
                deferred = facts.get("deferred", 0)
                absent = facts.get("absent", 0)
                entry["detail"] = (
                    f"{facts['enriched']:,} enriched, {facts.get('rated', 0):,} rated"
                    + (f", {absent:,} not in MDBList" if absent else "")
                    + (f", {deferred:,} deferred to the next run" if deferred else "")
                )
                entry["stats"] = facts

            elif key == "retag":
                from workers.retag import run_retag

                def _retag_progress(processed: int, tags: int, cost: float) -> None:
                    entry["detail"] = (
                        f"{processed:,} titles, {tags:,} tags, ~${cost:.2f}"
                    )
                    emit(entry)

                stats = await run_retag(
                    None,
                    limit,
                    min_votes,
                    False,
                    enriched_only=True,
                    source_done=enrich_done,
                    on_progress=_retag_progress,
                )
                # "~" when the token counts came from the character-length
                # fallback rather than the API, so the figure is never read as
                # metered when it is not.
                tilde = "~" if stats.get("estimated_usage") else ""
                entry["detail"] = (
                    f"{stats.get('processed', 0):,} titles, "
                    f"{stats.get('tags_written', 0):,} tags, "
                    f"{tilde}${stats.get('cost_usd', 0.0):.2f}"
                )
                entry["stats"] = stats
                report["cost_usd"] = stats.get("cost_usd", 0.0)
                if stats.get("halted"):
                    raise RuntimeError(
                        str(
                            stats.get("halt_reason")
                            or stats.get("first_error")
                            or "Tagging safety circuit opened"
                        )
                    )
                if stats.get("failed") and not stats.get("processed"):
                    raise RuntimeError(
                        str(
                            stats.get("first_error")
                            or f"All {stats['failed']:,} tagging attempts failed"
                        )
                    )

            elif key == "relevance":
                from workers.daily_update import refresh_relevance_threaded

                counts = await asyncio.to_thread(refresh_relevance_threaded, "upgrade")
                entry["detail"] = (
                    f"{counts['tags']:,} tag stats, {counts['rows']:,} rows weighted"
                )
                entry["stats"] = counts

            elif key == "catalogs":
                from app.category_synth import rebuild_synthesized_shelves

                def _regen() -> int:
                    with get_db() as db:
                        require_catalog_publication_ready(db)
                        rebuild_synthesized_shelves(db)

                        count = (
                            db.query(func.count(UniversalCatalogContent.tmdb_id))
                            .join(
                                UniversalCategory,
                                UniversalCategory.id
                                == UniversalCatalogContent.category_id,
                            )
                            .filter(UniversalCategory.source == "generated")
                            .scalar()
                            or 0
                        )

                    # Serving reads go through an in-process LRU with a
                    # cache_ttl (default 1h) lifetime, so without this the whole
                    # upgrade stays invisible for up to an hour after it reports
                    # done -- which reads as "the run did nothing".
                    from app.main import _catalog_cache

                    _catalog_cache.clear()
                    logger.info("upgrade: catalog cache cleared")
                    return count

                items = await asyncio.to_thread(_regen)
                entry["detail"] = f"{items:,} synthesized catalog items"
                entry["stats"] = {"items": items}

            elif key == "users":
                # Without this the run reports "done" while every per-user row
                # -- byw-*, taste-*, picks-*, rec-*, gems-*, up-next -- is still
                # serving content built from the OLD tags, until the nightly
                # scheduler happens to fire. The plan's own Phase 4 ordering
                # ends refresh_tag_stats -> regenerate_all_universal_catalogs ->
                # sync_all_users; only the third was missing.
                from workers.personalized_sync import sync_all_users

                with get_db() as db:
                    synced = await sync_all_users(db)
                entry["detail"] = (
                    f"{synced.get('synced', 0):,}/{synced.get('total', 0):,} profiles, "
                    f"{synced.get('catalogs', 0):,} rows"
                    + (f", {synced['failed']:,} failed" if synced.get("failed") else "")
                )
                entry["stats"] = synced

            if entry["status"] == "running":
                entry["status"] = "completed"

        except BuildStopped:
            entry.update(status="stopped", detail="Stopped by request")
            report["stopped"] = True
            logger.warning(f"upgrade: {key} stopped by request")
        except Exception as exc:  # noqa: BLE001 - one bad step must not end the run
            entry.update(status="failed", detail=f"{type(exc).__name__}: {exc}"[:400])
            if key in {"retag", "relevance", "catalogs"}:
                report["publication_blocked"] = True
            logger.error(f"upgrade step {key} failed: {exc}")

        entry["completed_at"] = utc_now().isoformat()
        emit(entry)

    await asyncio.gather(*(run_step(k) for k in CONCURRENT))
    # Both sources are done -- including on failure, which is the point: a TMDB
    # or MDBList outage must not leave the tagger's re-query loop waiting for
    # titles that are never coming.
    enrich_done.set()

    for key in SEQUENTIAL:
        if report["stopped"] or stop_event.is_set():
            # Deliberate: relevance and catalogs are what make a run visible to
            # users. Skipping them after a stop leaves the coherent pre-upgrade
            # rows in place rather than publishing a half-retagged library.
            entries[key].update(status="stopped", detail="Skipped — run stopped")
            emit(entries[key])
            continue
        if report["publication_blocked"]:
            entries[key].update(
                status="blocked",
                detail="Skipped — required upstream publication step failed",
                completed_at=utc_now().isoformat(),
            )
            emit(entries[key])
            continue
        await run_step(key)

    report["completed_at"] = utc_now().isoformat()
    report["duration_s"] = int((utc_now() - started).total_seconds())
    ok = sum(1 for s in report["steps"] if s["status"] == "completed")
    logger.info(
        f"upgrade finished: {ok}/{len(report['steps'])} steps in "
        f"{report['duration_s']}s"
    )
    return report
