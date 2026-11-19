"""
Cost-bounded rebuild — re-tag the recommendation-eligible catalog against the
current taxonomy.

    python workers/retag.py --estimate            # count + projected cost, no API calls
    python workers/retag.py --dry-run             # render 2 real prompts, no API calls
    python workers/retag.py                       # the rebuild
    python workers/retag.py --limit 5000          # partial
    python workers/retag.py --min-votes 2000      # optional tighter pilot scope
    python workers/retag.py --all-titles          # explicit, usually wasteful opt-out
    python workers/retag.py --tmdb-ids 603,1396 --force   # a scoped validation pass

The default scope is the same quality-eligible corpus that can appear in a
catalog. The much larger acquisition tail remains searchable and can be tagged
explicitly with ``--all-titles``, but a normal rebuild does not spend model
budget classifying titles no recommendation row is allowed to show.

**Resumable by construction.** Selection is "titles with neither tags nor a
terminal attempt at the current tagger_version" and the job takes one immutable
snapshot. A killed run re-runs the query and skips what landed. Evidence-poor
titles are dispositioned for this version instead of becoming a paid retry
backlog; a version bump or explicit force run may reconsider them. Provider
errors keep the deterministic factual set and do not create retries.

**Browsing is untouched throughout.** Catalogs are served from the pre-computed
``universal_catalog_content`` / ``user_catalog_content`` snapshots, not from live
tag joins, and those are regenerated once at the end. Within a title the swap is
atomic (see ``tagging_common.tag_and_store``), so no query ever sees a title
mid-rebuild.

**Popularity order.** An interrupted run leaves the part of the library that
actually surfaces in rows finished first.
"""

import argparse
import asyncio
import sys
from pathlib import Path

from loguru import logger
from sqlalchemy import func
from sqlalchemy.orm import defer

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.time_utils import utc_now  # noqa: E402
from app.config import settings  # noqa: E402
from app.database import get_db, init_database  # noqa: E402
from app.models import MediaMetadata, MovieTag, TaggingJob  # noqa: E402
from workers.build_control import (  # noqa: E402
    BuildStopped,
    check_pause,
    reset_build_stop,
    stop_event,
)
from workers.tagging_common import (  # noqa: E402
    build_features,
    effective_batch_size,
    estimate_cost,
    tagger_version,
)

#: Conservative pre-flight allowance for each title that reaches the optional
#: interpretive model. The live job replaces it with measured usage (or a
#: conservative reservation after a failed attempt).
EST_INPUT_TOKENS = 2500
EST_OUTPUT_TOKENS = 1200

# Offline replay of jobs 225, 226 and 227 admitted 57.2%, 57.3% and 57.2% of
# titles respectively. Round upward so Admin Center remains a budget guard.
AI_ADMISSION_RATE_ESTIMATE = 0.60

# Requests are grouped by media type, primary genre and decade before being
# chunked, so many buckets end with a partially filled batch.  A live 5,000-title
# pilot at batch size 20 produced 417 requests (12.0 titles/request), not the 250
# implied by perfect packing.  This affects quota and duration, not token cost.
EST_TITLES_PER_REQUEST = 12


def _estimated_requests(pending: int, batch_size: int) -> int:
    """Estimate calls after taxonomy buckets leave partially filled batches."""
    if pending <= 0:
        return 0
    estimated_fill = min(max(1, batch_size), EST_TITLES_PER_REQUEST)
    return -(-pending // estimated_fill)


def estimated_ai_titles(pending: int) -> int:
    """Conservative count of titles expected to require paid interpretation."""
    if pending <= 0:
        return 0
    return min(pending, int(pending * AI_ADMISSION_RATE_ESTIMATE + 0.999999))


def _pending_query(
    db,
    media_type=None,
    min_votes=0,
    force=False,
    enriched_only=False,
    stale_facts=False,
    tmdb_ids=None,
    eligible_only=True,
):
    """Titles that need tagging at the current version.

    ``stale_facts`` selects a different set entirely: titles that ARE at the
    current version but whose tags were produced before their MDBList facts
    landed, i.e. ``tagged_at < mdb_enriched_at``. Those tags never saw the
    reviews, and no version bump would find them, because the taxonomy did not
    change -- only the inputs did. It is the precise answer to "re-tag what the
    broken enrichment affected, and nothing else".

    ``tmdb_ids`` restricts the selection to an explicit list, which is what
    makes a *validation pass* affordable. Promoting an alias is not retroactive
    and cannot be scoped precisely from what we store -- ``TagObservation``
    keeps a bounded sample per string, not the full affected set -- so the
    choice used to be a whole-library re-tag (~$25) or nothing. Re-tagging just
    the samples costs cents and answers the only question that matters before
    committing: did the promotion actually change the output? Combine with
    ``force``, since the samples are already at the current version.
    """
    version = tagger_version()
    query = (
        db.query(MediaMetadata)
        # raw_data is a ~5KB TMDB payload after enrichment and build_features
        # never reads it, so selecting it would deserialize gigabytes of JSON
        # for nothing -- at 350k pending rows that is minutes of stall and a
        # real OOM risk. Three other call sites defer it for the same reason
        # (app/catalog_generator.py:555, workers/daily_update.py:298,
        # workers/personalized_sync.py:873); this one had been missed.
        .options(defer(MediaMetadata.raw_data)).order_by(
            MediaMetadata.popularity.desc().nullslast()
        )
    )
    if media_type:
        query = query.filter(MediaMetadata.media_type == media_type)
    if eligible_only:
        # The tag corpus serves recommendations, not acquisition/storage. Paying
        # to semantically classify titles the strict catalog gate can never show
        # was most of the old rebuild's cost and amplified the junk tail.
        from app.catalog_generator import quality_floor_clauses

        clauses = quality_floor_clauses()
        if clauses:
            query = query.filter(*clauses)
    if tmdb_ids is not None:
        # An empty list means "nothing", not "everything" -- a caller that
        # computed an empty set should get an empty run, not the library.
        query = query.filter(MediaMetadata.tmdb_id.in_(list(tmdb_ids)))
    if enriched_only:
        # Lets the re-tag run *alongside* enrichment rather than after it.
        # Both stages walk the library popularity-descending, so the tagger
        # simply trails the enricher and re-queries as more titles become
        # eligible. The dependency that motivated the ordering is preserved
        # exactly -- no title is ever tagged before its keywords, cast and
        # certification exist -- while the serialisation is not.
        from workers.enrich import ENRICH_VERSION

        query = query.filter(MediaMetadata.enrich_version == ENRICH_VERSION)
    if min_votes:
        # The universal catalogs apply a vote_count floor (default 10), so a
        # title below it cannot appear in a row however well it is tagged.
        query = query.filter(func.coalesce(MediaMetadata.vote_count, 0) >= min_votes)
    if stale_facts:
        # Tagged at the current version, but before its MDBList facts arrived.
        stale = (
            db.query(
                MovieTag.tmdb_id,
                MovieTag.media_type,
                func.max(MovieTag.tagged_at).label("tagged_at"),
            )
            .filter(MovieTag.tagger_version == version)
            .group_by(MovieTag.tmdb_id, MovieTag.media_type)
            .subquery()
        )
        return (
            query.join(
                stale,
                (MediaMetadata.tmdb_id == stale.c.tmdb_id)
                & (MediaMetadata.media_type == stale.c.media_type),
            )
            .filter(MediaMetadata.mdb_enriched_at.isnot(None))
            .filter(stale.c.tagged_at < MediaMetadata.mdb_enriched_at)
        )

    if not force:
        current = (
            db.query(MovieTag.tmdb_id, MovieTag.media_type)
            .filter(MovieTag.tagger_version == version)
            .distinct()
            .subquery()
        )
        query = query.outerjoin(
            current,
            (MediaMetadata.tmdb_id == current.c.tmdb_id)
            & (MediaMetadata.media_type == current.c.media_type),
        ).filter(current.c.tmdb_id.is_(None))
        query = query.filter(
            (MediaMetadata.tag_attempt_version.is_(None))
            | (MediaMetadata.tag_attempt_version != version)
        )
    return query


def estimate(
    media_type=None,
    min_votes=0,
    force=False,
    tmdb_ids=None,
    eligible_only=True,
    limit=None,
) -> dict:
    """Size the job before spending anything."""
    with get_db() as db:
        pending = _pending_query(
            db,
            media_type,
            min_votes,
            force,
            tmdb_ids=tmdb_ids,
            eligible_only=eligible_only,
        ).count()
        if limit is not None:
            pending = min(pending, max(0, int(limit)))
        total_query = db.query(func.count(MediaMetadata.tmdb_id))
        if media_type:
            total_query = total_query.filter(MediaMetadata.media_type == media_type)
        total = total_query.scalar() or 0
        eligible_query = db.query(func.count(MediaMetadata.tmdb_id))
        if media_type:
            eligible_query = eligible_query.filter(
                MediaMetadata.media_type == media_type
            )
        if eligible_only:
            from app.catalog_generator import quality_floor_clauses

            eligible_clauses = quality_floor_clauses()
            if eligible_clauses:
                eligible_query = eligible_query.filter(*eligible_clauses)
        eligible = eligible_query.scalar() or 0
        enriched = (
            db.query(func.count(MediaMetadata.tmdb_id))
            .filter(MediaMetadata.tag_facts.isnot(None))
            .scalar()
            or 0
        )
        with_mdb = (
            db.query(func.count(MediaMetadata.tmdb_id))
            .filter(MediaMetadata.mdb_facts.isnot(None))
            .scalar()
            or 0
        )

    batch_size = effective_batch_size()
    ai_pending = estimated_ai_titles(pending)
    requests = _estimated_requests(ai_pending, batch_size)
    tokens_in = ai_pending * EST_INPUT_TOKENS
    tokens_out = ai_pending * EST_OUTPUT_TOKENS
    cost = estimate_cost(tokens_in, tokens_out)

    from app.tagging_provider import (
        active_model,
        normalize_provider,
        production_model_ready,
    )

    report = {
        "library": total,
        "eligible": eligible,
        "excluded_by_quality": max(0, total - eligible) if eligible_only else 0,
        "pending": pending,
        "estimated_ai_titles": ai_pending,
        "estimated_local_only": pending - ai_pending,
        "enriched": enriched,
        "with_mdb_facts": with_mdb,
        "requests": requests,
        "batch_size": batch_size,
        "est_input_tokens": tokens_in,
        "est_output_tokens": tokens_out,
        "est_cost_usd": round(cost, 2),
        "provider": normalize_provider(),
        "model": active_model(),
        "model_production_ready": production_model_ready(),
        "tagger_version": tagger_version(),
        "cost_ceiling_usd": settings.retag_cost_ceiling_usd,
        "cost_basis": "taxonomy-v5-offline-replay-conservative",
        "catalog_min_quality": settings.catalog_min_quality,
        "scope": "quality-eligible" if eligible_only else "all titles",
    }
    logger.info("=" * 60)
    for key, value in report.items():
        logger.info(
            f"  {key:<20} {value:,}"
            if isinstance(value, int)
            else f"  {key:<20} {value}"
        )
    logger.info("=" * 60)
    if cost > settings.retag_cost_ceiling_usd:
        logger.warning(
            f"Projected ${cost:,.2f} exceeds the ${settings.retag_cost_ceiling_usd} "
            "ceiling — the run would halt partway. Raise retag_cost_ceiling_usd."
        )
    return report


async def dry_run(
    sample: int = 2, media_type=None, min_votes=0, eligible_only=True
) -> int:
    """Render real prompts without calling the API.

    Reading the prompt before spending is the cheapest quality check available:
    it shows exactly what the model will see, including whether enrichment
    actually populated keywords, cast and certification.
    """
    from app.tagging_prompt import build_prompt
    from app.taxonomy import estimate_tokens, group_into_batches

    with get_db() as db:
        rows = (
            _pending_query(db, media_type, min_votes, eligible_only=eligible_only)
            .limit(sample * effective_batch_size())
            .all()
        )
        features = [build_features(row) for row in rows]

    if not features:
        logger.warning("Nothing pending — the library is already at this version.")
        return 0

    for index, batch in enumerate(
        group_into_batches(features, effective_batch_size()), start=1
    ):
        if index > sample:
            break
        prompt, vocab = build_prompt(batch)
        logger.info("=" * 72)
        logger.info(
            f"BATCH {index}: {len(batch)} titles, {len(vocab)} tags offered, "
            f"~{estimate_tokens(prompt):,} tokens"
        )
        logger.info("=" * 72)
        print(prompt[:6000])
        if len(prompt) > 6000:
            print(f"\n... [{len(prompt) - 6000} more characters]")
    return 0


async def run_retag(
    media_type=None,
    limit=None,
    min_votes=0,
    force=False,
    enriched_only=False,
    source_done=None,
    stale_facts=False,
    on_progress=None,
    tmdb_ids=None,
    eligible_only=True,
) -> dict:
    """The rebuild.

    Args:
        enriched_only: Only consider titles TMDB enrichment has finished with.
        stale_facts: Re-tag titles already at the current version whose tags
            predate their MDBList facts. Deterministic source, franchise and
            pedigree facts may have changed, and nothing else would select
            those titles.
        tmdb_ids: Restrict to an explicit id list. Combined with ``force`` this
            is the cheap validation pass after promoting aliases -- see
            :func:`_pending_query`.
        source_done: An ``asyncio.Event`` set when enrichment finishes. Tagging
            waits for it before taking one immutable work snapshot. The former
            overlapping design tagged titles before MDB facts landed, then paid
            for a second stale-facts pass over the same titles.
        on_progress: Called with running ``(processed, tags_written, cost_usd)``
            so a caller can render live progress. Separate from the internal
            ``TaggingJob`` row update, which is a database write on a fixed
            cadence rather than a display hook.

    The caller owns the stop flag. This used to call ``reset_build_stop()``
    itself, which was harmless while it ran alone but silently clears a stop the
    operator pressed during enrichment now that the two overlap.
    """
    from app.tagging_provider import active_model, normalize_provider

    started = utc_now()

    with get_db() as db:
        job = TaggingJob(
            job_type="manual_retag",
            started_at=started,
            status="running",
            job_metadata={
                "target_version": tagger_version(),
                "provider": normalize_provider(),
                "model": active_model(),
                "media_type": media_type or "all",
                "min_votes": min_votes,
                "eligible_only": eligible_only,
                "mode": (
                    "stale_facts"
                    if stale_facts
                    else (
                        "scoped"
                        if tmdb_ids is not None
                        else "force" if force else "pending"
                    )
                ),
                # The count, not the ids: a scoped run can carry thousands and
                # the job row is read by the admin UI on a poll.
                "scoped_ids": len(tmdb_ids) if tmdb_ids is not None else None,
            },
        )
        db.add(job)
        db.commit()
        job_id = job.id

    from workers.tagging_common import tag_and_store

    def progress(inner: dict) -> None:
        if on_progress:
            try:
                on_progress(
                    totals["processed"] + inner["processed"],
                    totals["tags_written"] + inner["tags_written"],
                    totals["cost_usd"] + inner["cost_usd"],
                )
            except Exception:  # noqa: BLE001 - progress must never fail the run
                pass
        if inner["batch"] % 25:
            return
        with get_db() as db:
            job = db.query(TaggingJob).filter(TaggingJob.id == job_id).first()
            if job:
                job.items_processed = totals["processed"] + inner["processed"]
                job.items_failed = (
                    totals["failed"]
                    + totals["requeued"]
                    + inner["failed"]
                    + inner["requeued"]
                )
                job.total_api_calls = totals["api_calls"] + inner["api_calls"]
                job.total_api_attempts = totals["api_attempts"] + inner["api_attempts"]
                job.input_tokens = totals["input_tokens"] + inner["input_tokens"]
                job.output_tokens = totals["output_tokens"] + inner["output_tokens"]
                job.cached_tokens = totals["cached_tokens"] + inner["cached_tokens"]
                job.thinking_tokens = (
                    totals["thinking_tokens"] + inner["thinking_tokens"]
                )
                job.usage_estimated = bool(
                    totals["estimated_usage"] or inner["estimated_usage"]
                )
                job.pricing_basis = inner.get("pricing_basis") or None
                job.total_tokens_used = (
                    totals["input_tokens"]
                    + totals["output_tokens"]
                    + inner["input_tokens"]
                    + inner["output_tokens"]
                )
                job.estimated_cost = round(totals["cost_usd"] + inner["cost_usd"], 4)
                db.commit()

    def _load(remaining: int | None) -> list:
        with get_db() as db:
            query = _pending_query(
                db,
                media_type,
                min_votes,
                force,
                enriched_only,
                stale_facts,
                tmdb_ids,
                eligible_only,
            )
            if remaining:
                query = query.limit(remaining)
            return [build_features(row) for row in query.all()]

    totals: dict = {
        "processed": 0,
        "failed": 0,
        "tags_written": 0,
        "off_vocabulary": 0,
        "unresolved_tags": 0,
        "extra_observations": 0,
        "requeued": 0,
        "terminal_skips": 0,
        "evidence_skips": 0,
        "missing_results": 0,
        "model_omissions": 0,
        "provider_failures": 0,
        "model_skips": 0,
        "missing_retry_calls": 0,
        "recovered_missing_results": 0,
        "low_tag_results": 0,
        "low_tag_retry_calls": 0,
        "recovered_low_tag_results": 0,
        "low_tag_diagnostics": [],
        "observation_samples": [],
        "blocked": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "api_calls": 0,
        "api_attempts": 0,
        "cached_tokens": 0,
        "thinking_tokens": 0,
        "cost_usd": 0.0,
        "estimated_usage": False,
        "first_error": "",
        "halt_reason": "",
        "halted": False,
        "pricing_basis": "",
    }
    stats: dict = {}

    try:
        if source_done is not None:
            while not source_done.is_set() and not stop_event.is_set():
                await check_pause()
                await asyncio.sleep(1)

        if not stop_event.is_set():
            # One immutable snapshot, one attempt per title. A validator
            # rejection gets a terminal disposition for this tagger version;
            # it can only be retried by Force or by deploying a new version.
            features = _load(limit)
        else:
            features = []
        if features:
            logger.info(
                f"retag: {len(features):,} titles at version {tagger_version()}"
            )
            with get_db() as db:
                stats = await tag_and_store(
                    db,
                    features,
                    replace_all=stale_facts,
                    on_progress=progress,
                    should_stop=stop_event.is_set,
                    check_pause=check_pause,
                    spent_usd=0.0,
                )
            for key, value in stats.items():
                if key in ("first_error", "halt_reason"):
                    if value and not totals[key]:
                        totals[key] = value
                elif key == "estimated_usage":
                    # Sticky: once any pass fell back to character-length
                    # tokens, the run total is an estimate.
                    totals["estimated_usage"] = totals["estimated_usage"] or value
                elif key == "pricing_basis":
                    totals[key] = str(value)
                elif key in ("low_tag_diagnostics", "observation_samples"):
                    totals[key].extend(list(value)[: 50 - len(totals[key])])
                elif key in totals and isinstance(value, (int, float)):
                    totals[key] += value
    except BuildStopped:
        logger.warning("retag: stopped by request")

    stats = totals

    with get_db() as db:
        job = db.query(TaggingJob).filter(TaggingJob.id == job_id).first()
        if job:
            job.completed_at = utc_now()
            job.items_processed = stats.get("processed", 0)
            # Requeued titles did not receive a usable tag set. Surface them in
            # the operator-facing failure count as well as the detailed job
            # metadata; otherwise a partial-quality failure looks complete.
            job.items_failed = stats.get("failed", 0) + stats.get("requeued", 0)
            job.total_api_calls = stats.get("api_calls", 0)
            job.total_api_attempts = stats.get("api_attempts", 0)
            job.input_tokens = stats.get("input_tokens", 0)
            job.output_tokens = stats.get("output_tokens", 0)
            job.cached_tokens = stats.get("cached_tokens", 0)
            job.thinking_tokens = stats.get("thinking_tokens", 0)
            job.usage_estimated = bool(stats.get("estimated_usage"))
            job.pricing_basis = stats.get("pricing_basis") or None
            job.total_tokens_used = stats.get("input_tokens", 0) + stats.get(
                "output_tokens", 0
            )
            job.estimated_cost = round(stats.get("cost_usd", 0.0), 4)
            if stats.get("halted"):
                job.status = "failed"
                job.error_message = str(
                    stats.get("halt_reason")
                    or stats.get("first_error")
                    or "Tagging safety circuit opened"
                )[:2000]
            elif stats.get("failed") and not stats.get("processed"):
                job.status = "failed"
                job.error_message = (
                    f"All {stats['failed']} items failed. First error: "
                    f"{stats.get('first_error') or 'unknown'}"
                )[:2000]
            elif stop_event.is_set():
                job.status = "cancelled"
            else:
                job.status = "completed"
                # Optional interpretive-provider problems are diagnostics, not
                # completed-job errors. Showing them in the red error slot made
                # successful jobs look failed in Admin Center.
                if stats.get("failed") and stats.get("first_error"):
                    job.error_message = str(stats["first_error"])[:2000]
                else:
                    job.error_message = None
            meta = dict(job.job_metadata or {})
            meta.update(
                {
                    "tags_written": stats.get("tags_written", 0),
                    "off_vocabulary": stats.get("off_vocabulary", 0),
                    "unresolved_tags": stats.get("unresolved_tags", 0),
                    "extra_observations": stats.get("extra_observations", 0),
                    "requeued": stats.get("requeued", 0),
                    "terminal_skips": stats.get("terminal_skips", 0),
                    "evidence_skips": stats.get("evidence_skips", 0),
                    "missing_results": stats.get("missing_results", 0),
                    "model_omissions": stats.get("model_omissions", 0),
                    "provider_failures": stats.get("provider_failures", 0),
                    "model_skips": stats.get("model_skips", 0),
                    "missing_retry_calls": stats.get("missing_retry_calls", 0),
                    "recovered_missing_results": stats.get(
                        "recovered_missing_results", 0
                    ),
                    "low_tag_results": stats.get("low_tag_results", 0),
                    "low_tag_retry_calls": stats.get("low_tag_retry_calls", 0),
                    "recovered_low_tag_results": stats.get(
                        "recovered_low_tag_results", 0
                    ),
                    "low_tag_diagnostics": stats.get("low_tag_diagnostics", []),
                    "observation_samples": stats.get("observation_samples", []),
                    "halt_reason": stats.get("halt_reason", ""),
                    "first_batch_error": stats.get("first_error", ""),
                    "api_attempts": stats.get("api_attempts", 0),
                    "input_tokens": stats.get("input_tokens", 0),
                    "output_tokens": stats.get("output_tokens", 0),
                    "cached_tokens": stats.get("cached_tokens", 0),
                    "thinking_tokens": stats.get("thinking_tokens", 0),
                    "estimated_usage": bool(stats.get("estimated_usage")),
                    "pricing_basis": stats.get("pricing_basis", ""),
                    "eligible_only": eligible_only,
                }
            )
            job.job_metadata = meta
            db.commit()

    logger.info("=" * 60)
    outcome = "HALTED" if stats.get("halted") else "COMPLETE"
    logger.info(f"RETAG {outcome} — {stats.get('processed', 0):,} titles tagged")
    logger.info(f"  tags written    {stats.get('tags_written', 0):,}")
    logger.info(
        f"  observations    {stats.get('off_vocabulary', 0):,} "
        f"({stats.get('unresolved_tags', 0):,} unresolved tag names, "
        f"{stats.get('extra_observations', 0):,} extra concepts)"
    )
    logger.info(f"  API calls       {stats.get('api_calls', 0):,}")
    logger.info(
        f"  local-only      {stats.get('model_skips', 0):,} titles; "
        f"{stats.get('model_omissions', 0):,} optional model omissions"
    )
    # "Measured" means the REST response carried usageMetadata. Timed-out and
    # failed attempts are conservatively reserved and make the run an estimate.
    label = "estimated cost " if stats.get("estimated_usage") else "measured cost  "
    logger.info(f"  {label} ${stats.get('cost_usd', 0.0):,.2f}")
    if stats.get("blocked"):
        logger.info(
            f"  content-blocked {stats.get('blocked', 0):,} optional model calls"
        )
    if stats.get("provider_failures"):
        logger.info(
            f"  provider errors {stats.get('provider_failures', 0):,} titles "
            "kept with deterministic tags"
        )
    if stats.get("requeued"):
        logger.info(
            f"  excluded        {stats.get('terminal_skips', 0):,} titles "
            f"for this version "
            f"({stats.get('missing_results', 0):,} missing results, "
            f"{stats.get('low_tag_results', 0):,} with too few valid tags)"
        )
    if stats.get("evidence_skips"):
        logger.info(
            f"  no synopsis      {stats.get('evidence_skips', 0):,} titles "
            "excluded before API spend"
        )
    if stats.get("low_tag_retry_calls"):
        logger.info(
            f"  low-tag retry   {stats.get('low_tag_retry_calls', 0):,} calls, "
            f"{stats.get('recovered_low_tag_results', 0):,} titles recovered"
        )
    if stats.get("failed"):
        logger.info(f"  hard failures   {stats.get('failed', 0):,} titles")
    if stats.get("halt_reason"):
        logger.error(f"  reason           {stats['halt_reason']}")
    logger.info("=" * 60)
    if not stats.get("halted"):
        logger.info("Next: workers/refresh_relevance.py, then regenerate catalogs.")
    return stats


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--estimate", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--media-type", choices=("movie", "tv"), default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--min-votes",
        type=int,
        default=0,
        help="Apply an additional TMDB vote threshold on top of the shared "
        "catalog quality gate.",
    )
    parser.add_argument(
        "--force", action="store_true", help="Re-tag even at the current version"
    )
    parser.add_argument(
        "--stale-facts",
        action="store_true",
        help="Re-tag only titles whose tags predate their MDBList facts. Use "
        "after fixing or backfilling enrichment: it re-tags exactly what the "
        "missing data affected, not the whole library.",
    )
    parser.add_argument(
        "--all-titles",
        action="store_true",
        help="Explicitly include titles that the catalog quality gate cannot show. "
        "The default rebuild tags only the recommendation-eligible corpus.",
    )
    parser.add_argument(
        "--tmdb-ids",
        default=None,
        help="Comma-separated tmdb ids to restrict the run to. Combine with "
        "--force for the validation pass after promoting aliases: re-tag the "
        "recorded sample titles for cents, then re-read the off-vocabulary "
        "report to confirm the promotion changed the output before buying a "
        "whole-library pass.",
    )
    args = parser.parse_args()

    tmdb_ids = None
    if args.tmdb_ids:
        tmdb_ids = [int(p) for p in args.tmdb_ids.split(",") if p.strip()]

    init_database()

    if args.estimate:
        estimate(
            args.media_type,
            args.min_votes,
            args.force,
            tmdb_ids,
            eligible_only=not args.all_titles,
            limit=args.limit,
        )
        return 0
    if args.dry_run:
        return await dry_run(
            2, args.media_type, args.min_votes, eligible_only=not args.all_titles
        )

    estimate(
        args.media_type,
        args.min_votes,
        args.force,
        tmdb_ids,
        eligible_only=not args.all_titles,
        limit=args.limit,
    )
    # The CLI owns the stop flag here; the admin pipeline resets it itself
    # before starting, so run_retag must not clear it on the way in.
    reset_build_stop()
    await run_retag(
        args.media_type,
        args.limit,
        args.min_votes,
        args.force,
        stale_facts=args.stale_facts,
        tmdb_ids=tmdb_ids,
        eligible_only=not args.all_titles,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
