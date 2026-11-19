"""
The one tagging path, shared by the build worker, the daily update and the re-tag.

There used to be two near-identical copies of ``tag_items_batch`` -- one in
``initial_build`` (batch 100, skips already-tagged) and one in ``daily_update``
(batch 50, no skip) -- which had drifted in exactly the ways duplicated code
does. This is the single implementation.

Three things it does that neither copy did:

**Upserts through a dialect branch.** Both copies imported
``sqlalchemy.dialects.postgresql.insert`` unconditionally, which is why the
tagging path could never be exercised by the SQLite test suite. There is now one
``upsert_movie_tags`` that picks the right construct.

**Versions its output.** ``tagger_version`` is what makes a re-tag resumable and
what lets an improved prompt reach titles that already have tags. The old skip
test was "has any tag at all", so no prompt improvement could ever land on the
existing library.

**Swaps a title's tags atomically.** The ``movie_tags`` primary key is
``(tmdb_id, media_type, tag_id)``, so old and new rows cannot coexist for one
title; deleting up front would empty the library for the length of the run.
Instead each title's stale rows are deleted and its new ones inserted in one
transaction, so a served query never sees a title mid-swap. Catalogs read
pre-computed snapshots and are regenerated once at the end, so browsing is
untouched throughout.
"""

from __future__ import annotations

import asyncio
import hashlib
from contextlib import contextmanager
from typing import Any, Callable, Sequence

from loguru import logger
from sqlalchemy import tuple_
from sqlalchemy.orm import Session

from app.time_utils import utc_now  # noqa: E402
from app.config import settings
from app.models import MediaMetadata, MovieTag, Tag, TagObservation
from app.tagging_prompt import build_prompt, shortlist_names
from app.tagging_prompt import SYSTEM_BLOCK
from app.tag_validation import (
    ABSOLUTE_MAX_PER_ITEM,
    FUZZY_CUTOFF,
    MAX_MODEL_TAGS,
    MIN_SURVIVORS,
    MODEL_FACET_CAPS,
    VALIDATION_SCHEMA_VERSION,
    validate_item_tags,
)
from app.taxonomy import TAXONOMY_VERSION, derive_tags
from app.taxonomy_sync import load_db_aliases


FEATURE_SCHEMA_VERSION = "5"

# Forty-title requests repeatedly returned only ~22 result objects even though
# the objects that did arrive were healthy. Smaller batches improve instruction
# adherence, bound the damage from one malformed response, and cost little here
# because input tokens are far cheaper than output tokens on Flash-Lite.
MAX_SAFE_TAGGING_BATCH_SIZE = 20
# Job 221 proved that schema-valid is not semantically valid: GPT-5 nano could
# fill a ten-title object, but mixed title evidence and defaulted to the same
# generic labels. Three titles preserve useful prompt amortisation while
# keeping the index/title cross-reference small enough for economy models.
MAX_OPENAI_TAGGING_BATCH_SIZE = 3
MAX_ANTHROPIC_TAGGING_BATCH_SIZE = 5
MAX_CATASTROPHIC_LOW_TAG_RATE = 0.90


def effective_batch_size(
    requested: int | None = None, provider: str | None = None
) -> int:
    """Configured batch size, capped at the production-verified safe maximum."""
    size = max(
        1,
        min(int(requested or settings.tagging_batch_size), MAX_SAFE_TAGGING_BATCH_SIZE),
    )
    selected_provider = str(provider or settings.tagging_provider).strip().lower()
    if selected_provider == "openai":
        size = min(size, MAX_OPENAI_TAGGING_BATCH_SIZE)
    elif selected_provider == "anthropic":
        size = min(size, MAX_ANTHROPIC_TAGGING_BATCH_SIZE)
    return size


def _circuit_breaker_reason(stats: dict[str, Any]) -> str:
    """Return the reason a tagging run should stop, or an empty string.

    A 200-title sample is enough to identify a catastrophically broken response
    shape, but not enough to distinguish a normal handful of difficult titles
    from a systemic quality regression. Production catalog strata have varied
    from roughly 2% to 6% requeues while still returning valid, high-density
    tags. Title-specific low-tag results are persisted as a terminal disposition
    for this tagger version: they receive no questionable tags and cannot become
    a paid retry backlog. Provider omissions are optional-model diagnostics and
    never enter this breaker. Keep the cheap early fuse for actual write or
    validation regressions, then apply tighter steady-state limits after a
    representative sample.
    """
    attempted = stats["processed"] + stats["failed"] + stats["requeued"]
    if not attempted:
        return ""

    has_requeue_detail = any(
        key in stats for key in ("missing_results", "low_tag_results", "blocked")
    )
    missing_results = (
        stats.get("missing_results", 0) if has_requeue_detail else stats["requeued"]
    )
    low_tag_results = stats.get("low_tag_results", 0)
    requeue_rate = missing_results / attempted
    low_tag_rate = low_tag_results / attempted
    failure_rate = stats["failed"] / attempted
    rates = (("requeue", requeue_rate), ("failure", failure_rate))

    if attempted >= settings.tag_catastrophic_min_items:
        for label, rate in rates:
            if rate > settings.tag_catastrophic_rate:
                return (
                    f"Halted: catastrophic {label} rate {rate:.1%} exceeded "
                    f"{settings.tag_catastrophic_rate:.1%} after "
                    f"{attempted:,} attempted titles"
                )

        # A completely broken prompt can return objects that all validate but
        # contain too few tags. Normal sparse-title rates and the one-time
        # cleanup of an old retry backlog are far below this emergency fuse.
        if low_tag_rate > MAX_CATASTROPHIC_LOW_TAG_RATE:
            return (
                f"Halted: catastrophic low-tag rate {low_tag_rate:.1%} exceeded "
                f"{MAX_CATASTROPHIC_LOW_TAG_RATE:.1%} after "
                f"{attempted:,} attempted titles"
            )

    if attempted >= settings.tag_circuit_breaker_min_items:
        if requeue_rate > settings.tag_max_requeue_rate:
            return (
                f"Halted: requeue rate {requeue_rate:.1%} exceeded "
                f"{settings.tag_max_requeue_rate:.1%} after "
                f"{attempted:,} attempted titles"
            )
        if failure_rate > settings.tag_max_failure_rate:
            return (
                f"Halted: failure rate {failure_rate:.1%} exceeded "
                f"{settings.tag_max_failure_rate:.1%} after "
                f"{attempted:,} attempted titles"
            )
    return ""


def _latch_halt(stats: dict[str, Any], reason: str) -> None:
    """Record the operator-facing stop cause without losing diagnostics.

    ``first_error`` intentionally keeps the earliest batch anomaly. That is
    useful for debugging, but it is not necessarily why the run stopped: the
    production pilot first isolated a content-blocked title, then later crossed
    the requeue breaker. Keeping a separate halt reason prevents the harmless
    earlier anomaly from masking the breaker in the log and Admin Center.
    """
    stats["halted"] = True
    stats["halt_reason"] = stats.get("halt_reason") or reason
    stats["first_error"] = stats.get("first_error") or reason


def _mark_terminal_attempt(
    db: Session,
    *,
    tmdb_id: int,
    media_type: str,
    version: str,
    status: str,
) -> None:
    """Disposition a deterministic non-result without writing low-quality tags."""
    db.query(MediaMetadata).filter(
        MediaMetadata.tmdb_id == tmdb_id,
        MediaMetadata.media_type == media_type,
    ).update(
        {
            MediaMetadata.tag_attempt_version: version,
            MediaMetadata.tag_attempt_status: status,
            MediaMetadata.tag_attempted_at: utc_now(),
        },
        synchronize_session=False,
    )


def tagger_version() -> str:
    """Version stamped on every row this module writes.

    The stamp includes every input that can materially change assignments:
    taxonomy, prompt, feature shape, validator thresholds, and model. Merely
    changing a prompt can no longer leave rows falsely labelled current.

    A changed stamp only makes rows *eligible* for an explicit rebuild; it does
    not launch one. ``workers.retag`` additionally scopes normal rebuilds to the
    quality-eligible corpus, so correctness does not imply paying to classify
    the entire 354k-title acquisition tail.
    """
    from app.tagging_provider import (
        RESPONSE_SCHEMA_VERSION,
        active_model,
        normalize_provider,
    )
    from app.gemini_client import RESPONSE_SCHEMA_VERSION as GEMINI_SCHEMA_VERSION
    from workers.enrich import ENRICH_VERSION
    from workers.mdb_sync import MDB_VERSION

    provider = normalize_provider()
    # Preserve the exact legacy Gemini stamp inputs. Deploying the provider
    # selector alone must not make a complete 105k-title Gemini corpus look
    # stale. External providers include their identity because equal-looking
    # model names across providers must never share a version.
    provider_contract = (
        (GEMINI_SCHEMA_VERSION, active_model())
        if provider == "gemini"
        else (RESPONSE_SCHEMA_VERSION, provider, active_model())
    )
    payload = "\n".join(
        (
            TAXONOMY_VERSION,
            FEATURE_SCHEMA_VERSION,
            provider_contract[0],
            ENRICH_VERSION,
            MDB_VERSION,
            SYSTEM_BLOCK,
            *provider_contract[1:],
            str(settings.tag_min_confidence),
            str(settings.tag_max_per_item),
            str(MIN_SURVIVORS),
            str(FUZZY_CUTOFF),
            VALIDATION_SCHEMA_VERSION,
            str(ABSOLUTE_MAX_PER_ITEM),
            str(MAX_MODEL_TAGS),
            repr(sorted(MODEL_FACET_CAPS.items())),
            str(effective_batch_size(provider=provider)),
        )
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
    return f"{TAXONOMY_VERSION.split('.')[0]}-{digest}"[:20]


def upsert_movie_tags(db: Session, rows: list[dict[str, Any]]) -> None:
    """Insert-or-update tag rows on either PostgreSQL or SQLite."""
    if not rows:
        return
    bind = db.get_bind()
    dialect = bind.dialect.name if bind is not None else ""

    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        stmt = pg_insert(MovieTag).values(rows)
    else:
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        stmt = sqlite_insert(MovieTag).values(rows)

    db.execute(
        stmt.on_conflict_do_update(
            index_elements=["tmdb_id", "media_type", "tag_id"],
            set_={
                "confidence": stmt.excluded.confidence,
                "weight": stmt.excluded.weight,
                "weight_norm": stmt.excluded.weight_norm,
                "tagged_at": stmt.excluded.tagged_at,
                "tagger_version": stmt.excluded.tagger_version,
                "source": stmt.excluded.source,
                "model": stmt.excluded.model,
            },
        )
    )


#: Sample tmdb ids kept per observation.
#:
#: Was 10, which made a scoped re-tag after an alias promotion impossible: for a
#: string seen 5,000 times we held 0.2% of the affected titles, biased toward
#: whichever were tagged first. 50 does not make the set complete -- nothing
#: short of a join table would -- but it makes the *validation* pass after a
#: promotion meaningfully representative, which is what the sample is actually
#: for. Cost is ~40 extra integers on the rows that reach the cap at all.
OBSERVATION_SAMPLE_CAP = 50


def record_observations(
    db: Session, unresolved: Sequence[tuple[str, str, int]]
) -> None:
    """Record off-vocabulary strings instead of discarding them.

    ``unresolved`` is ``[(raw_name, media_type, sample_tmdb_id)]``. This is the
    governance loop: the admin view ranks these by frequency and an operator
    promotes one to an alias without a redeploy.
    """
    from app.taxonomy import normalize
    from app.taxonomy_sync import observation_resolution_map

    now = utc_now()
    # A malformed response can repeat the same free-form string, and blocked
    # batch bisection can surface a title more than once inside one logical
    # operation. Occurrences mean affected titles, not raw duplicate emissions.
    unique = {
        (str(raw), str(media_type), int(tmdb_id))
        for raw, media_type, tmdb_id in unresolved
    }
    pending: dict[tuple[str, str], dict[str, Any]] = {}
    for raw, media_type, tmdb_id in unique:
        key = normalize(raw)[:200]
        if not key:
            continue
        entry = pending.setdefault(
            (key, media_type), {"raw": str(raw)[:200], "ids": []}
        )
        entry["ids"].append(tmdb_id)

    keys = list(pending)
    existing = {
        (row.normalized, row.media_type): row
        for row in (
            db.query(TagObservation)
            .filter(
                tuple_(TagObservation.normalized, TagObservation.media_type).in_(keys)
            )
            .all()
            if keys
            else []
        )
    }
    known = observation_resolution_map(db)
    for (key, media_type), entry in pending.items():
        row = existing.get((key, media_type))
        ids = list(dict.fromkeys(entry["ids"]))
        resolution, mapped_tag_id = known.get(key, ("unmapped", None))
        if row is None:
            db.add(
                TagObservation(
                    normalized=key,
                    media_type=media_type,
                    raw_name=entry["raw"],
                    occurrences=len(ids),
                    first_seen=now,
                    last_seen=now,
                    sample_tmdb_ids=ids[:OBSERVATION_SAMPLE_CAP],
                    resolution=resolution,
                    mapped_tag_id=mapped_tag_id,
                )
            )
        else:
            row.occurrences = (row.occurrences or 0) + len(ids)
            row.last_seen = now
            samples = list(row.sample_tmdb_ids or [])
            for tmdb_id in ids:
                if tmdb_id not in samples and len(samples) < OBSERVATION_SAMPLE_CAP:
                    samples.append(tmdb_id)
            row.sample_tmdb_ids = samples
            if row.resolution == "unmapped" and mapped_tag_id is not None:
                row.resolution = resolution
                row.mapped_tag_id = mapped_tag_id


MODEL_PRICES_PER_MTOK: dict[tuple[str, str], tuple[float, float]] = {
    # Standard (synchronous) API rates. Batch mode is deliberately not claimed
    # here because this worker does not submit Batch API jobs.
    ("gemini", "gemini-2.5-flash-lite"): (0.10, 0.40),
    ("gemini", "gemini-3.1-flash-lite-preview"): (0.25, 1.50),
    ("gemini", "gemini-3.5-flash-lite-preview"): (0.30, 2.50),
    ("gemini", "gemini-3.5-flash-preview"): (1.50, 9.00),
    ("gemini", "gemini-3.6-flash-preview"): (1.50, 7.50),
    ("anthropic", "claude-haiku-4-5-20251001"): (1.00, 5.00),
    ("anthropic", "claude-haiku-4-5"): (1.00, 5.00),
    ("anthropic", "claude-sonnet-4-6"): (3.00, 15.00),
    ("openai", "gpt-5-nano"): (0.05, 0.40),
    ("openai", "gpt-4.1-nano"): (0.10, 0.40),
    ("openai", "gpt-4o-mini"): (0.15, 0.60),
    ("openai", "gpt-5.4-nano"): (0.20, 1.25),
    ("openai", "gpt-5.4-mini"): (0.75, 4.50),
    ("openai", "gpt-5.6-luna"): (1.00, 6.00),
    # Use the upper published rate where model-guide and model-detail pages
    # differ, so a pricing-site transition can never weaken the spend ceiling.
    ("openai", "gpt-5.6-terra"): (2.50, 15.00),
    ("openai", "gpt-5.6-sol"): (5.00, 30.00),
}


def pricing_for_model(
    model: str | None = None, provider: str | None = None
) -> tuple[float, float, str]:
    """Return rates and whether they are exact or conservative fallback."""
    from app.tagging_provider import active_model, normalize_provider

    selected_provider = normalize_provider(provider)
    name = (model or active_model(selected_provider)).removeprefix("models/")
    key = (selected_provider, name)
    if key in MODEL_PRICES_PER_MTOK:
        return (*MODEL_PRICES_PER_MTOK[key], "model-table")
    # Unknown/brand-new model: never let a stale cheap environment rate weaken
    # the ceiling. The fallback is the larger of the configured values and the
    # most expensive known Flash input/output prices.
    if selected_provider == "gemini":
        return (
            max(float(settings.gemini_input_cost_per_mtok), 1.50),
            max(float(settings.gemini_output_cost_per_mtok), 9.00),
            "conservative-unknown-model",
        )
    # Do not let an unrecognized model weaken the hard ceiling. These are the
    # highest standard synchronous rates among the selectable provider models.
    if selected_provider == "local":
        return (0.0, 0.0, "local-model")
    fallback = {"anthropic": (3.00, 15.00), "openai": (5.00, 30.00)}
    return (*fallback[selected_provider], "conservative-unknown-model")


def estimate_cost(
    input_tokens: int,
    output_tokens: int,
    *,
    model: str | None = None,
    provider: str | None = None,
) -> float:
    """USD for a token count at verified or conservative model rates."""
    input_rate, output_rate, _ = pricing_for_model(model, provider)
    return input_tokens / 1e6 * input_rate + output_tokens / 1e6 * output_rate


def _session_factory_for(db: Session) -> Callable[[], Any]:
    """A per-batch session factory on the same engine as ``db``."""
    bind = db.get_bind()

    @contextmanager
    def factory():
        session = Session(bind=bind)
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    return factory


async def tag_and_store(
    db: Session,
    features: Sequence[dict[str, Any]],
    *,
    engine=None,
    batch_size: int | None = None,
    replace_stale: bool = True,
    replace_all: bool = False,
    on_progress: Callable[[dict], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    check_pause: Callable[[], Any] | None = None,
    session_factory: Callable[[], Any] | None = None,
    spent_usd: float = 0.0,
    deterministic_only: bool = False,
) -> dict[str, Any]:
    """Tag a sequence of feature dicts and write the results.

    Args:
        features: Items from :func:`build_features`.
        replace_stale: Delete each title's rows from a different tagger version
            before inserting. This is the atomic swap.
        should_stop: Polled between batches for a cooperative stop.
        deterministic_only: Write only the rule-derived tags (``source=
            "derived"``) and make no provider call. Used when no AI key is
            configured; those titles are re-tagged once one is.
        spent_usd: What the *caller's run* has already spent across earlier
            calls. Without it the cost ceiling is per-call rather than per-run.
            The returned ``cost_usd`` remains this call's spend only.

    Returns a stats dict including measured token usage and estimated cost.
    """
    from app.tagging_provider import active_model, get_tagging_engine
    from app.taxonomy import group_into_batches

    engine = engine or get_tagging_engine()
    engine_provider = getattr(engine, "provider_name", settings.tagging_provider)
    engine_provider = str(engine_provider).lower()
    if engine_provider == "google":
        engine_provider = "gemini"
    engine_model = getattr(engine, "model_name", active_model())
    size = effective_batch_size(batch_size, engine_provider)
    version = tagger_version()
    aliases = load_db_aliases(db)
    tag_ids = {t.name: t.id for t in db.query(Tag).all()}

    stats: dict[str, Any] = {
        "processed": 0,
        "failed": 0,
        "tags_written": 0,
        "off_vocabulary": 0,
        "unresolved_tags": 0,
        "extra_observations": 0,
        "requeued": 0,
        # Terminal, title-specific outcomes at this tagger version. Retain the
        # legacy ``requeued`` counter for API/job compatibility, but these are
        # deliberately not offered to the next ordinary run.
        "terminal_skips": 0,
        "evidence_skips": 0,
        "missing_results": 0,
        # Provider omissions are informational in taxonomy v5. Deterministic
        # facts still publish the title, so no paid recovery call is made.
        "model_omissions": 0,
        # A provider outage is a loss of optional nuance, not a title failure.
        # The deterministic layer still publishes the title in the same batch.
        "provider_failures": 0,
        # Titles with no evidence-backed interpretive candidates never enter a
        # paid request; their deterministic tags are written locally.
        "model_skips": 0,
        "missing_retry_calls": 0,
        "recovered_missing_results": 0,
        "low_tag_results": 0,
        "low_tag_retry_calls": 0,
        "recovered_low_tag_results": 0,
        # Bounded, job-scoped evidence for QA. The old aggregate counters could
        # say that 43 flagship titles failed without retaining whether the
        # cause was duplicate indexes, a facet cap or a conflict.
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
        # True once any call's tokens came from the character-length fallback
        # rather than the API, so a reader knows the spend is approximate.
        "estimated_usage": False,
        "first_error": "",
        "halt_reason": "",
        "halted": False,
        "provider": engine_provider,
        "model": engine_model,
        "pricing_basis": pricing_for_model(engine_model, engine_provider)[2],
        "_pricing_provider": engine_provider,
        "_pricing_model": engine_model,
    }

    # Honour an already-latched operator stop before doing even local writes.
    # The between-batch check below handles stops that arrive during a run.
    if should_stop and should_stop():
        stats.pop("_pricing_provider", None)
        stats.pop("_pricing_model", None)
        return stats

    # Concurrent batches each need their own session, but they must live on the
    # same database as the one handed in -- deriving the factory from `db`'s
    # bind rather than reaching for the global get_db() keeps a caller that
    # injected a session (every test here, and any future caller working
    # against a non-default engine) writing where it expects to.
    session_factory = session_factory or _session_factory_for(db)

    from app.tag_evidence import has_prompt_evidence

    sparse: list[dict[str, Any]] = []
    local_only: list[dict[str, Any]] = []
    admitted: list[dict[str, Any]] = []
    for original in features:
        item = dict(original)
        item["_deterministic_tags"] = derive_tags(item)
        if not item["_deterministic_tags"]:
            sparse.append(item)
        elif deterministic_only:
            local_only.append(item)
        elif has_prompt_evidence(item) and shortlist_names(item):
            admitted.append(item)
        else:
            local_only.append(item)

    if local_only:
        rows: list[dict[str, Any]] = []
        now = utc_now()
        with session_factory() as local_db:
            for item in local_only:
                result = validate_item_tags(
                    int(item["tmdb_id"]),
                    str(item["media_type"]),
                    [],
                    shortlist_names=[],
                    db_aliases=aliases,
                    derived=item["_deterministic_tags"],
                    min_confidence=settings.tag_min_confidence,
                    max_per_item=settings.tag_max_per_item,
                )
                if result.requeue:
                    sparse.append(item)
                    continue

                tmdb_id = int(item["tmdb_id"])
                media_type = str(item["media_type"])
                if item.get("_tag_attempt_version") is not None:
                    local_db.query(MediaMetadata).filter(
                        MediaMetadata.tmdb_id == tmdb_id,
                        MediaMetadata.media_type == media_type,
                    ).update(
                        {
                            MediaMetadata.tag_attempt_version: None,
                            MediaMetadata.tag_attempt_status: None,
                            MediaMetadata.tag_attempted_at: None,
                        },
                        synchronize_session=False,
                    )
                if replace_stale:
                    conditions = [
                        MovieTag.tmdb_id == tmdb_id,
                        MovieTag.media_type == media_type,
                    ]
                    if not replace_all:
                        conditions.append(
                            (MovieTag.tagger_version.is_(None))
                            | (MovieTag.tagger_version != version)
                        )
                    local_db.query(MovieTag).filter(*conditions).delete(
                        synchronize_session=False
                    )
                for accepted in result.accepted:
                    if accepted.tag is None:
                        continue
                    tag_id = tag_ids.get(accepted.tag.name)
                    if tag_id is None:
                        continue
                    rows.append(
                        {
                            "tmdb_id": tmdb_id,
                            "media_type": media_type,
                            "tag_id": tag_id,
                            "confidence": accepted.confidence,
                            "weight": None,
                            "weight_norm": None,
                            "tagged_at": now,
                            "tagger_version": version,
                            "source": "derived",
                            "model": None,
                        }
                    )
                stats["processed"] += 1
            upsert_movie_tags(local_db, rows)
            local_db.commit()
        stats["tags_written"] += len(rows)
        stats["model_skips"] += len(local_only)
        logger.info(
            f"tagging: wrote {len(local_only):,} deterministic-only title(s) "
            "without a provider call"
        )

    if sparse:
        # Do not ask a model to infer viewing experience from a title and broad
        # genres. Persist a versioned terminal disposition so nightly/initial
        # jobs do not pay to reconsider the same evidence-poor record forever.
        with session_factory() as terminal_db:
            for item in sparse:
                _mark_terminal_attempt(
                    terminal_db,
                    tmdb_id=int(item["tmdb_id"]),
                    media_type=str(item["media_type"]),
                    version=version,
                    status="insufficient_source_evidence",
                )
            terminal_db.commit()
        stats["terminal_skips"] += len(sparse)
        stats["evidence_skips"] += len(sparse)
        logger.info(
            f"tagging: excluded {len(sparse):,} title(s) with no usable "
            "deterministic or interpretive evidence"
        )

    batches = list(group_into_batches(admitted, size))
    concurrency = max(1, settings.gemini_concurrency)
    semaphore = asyncio.Semaphore(concurrency)
    halted = False
    completed = 0
    logger.info(
        f"tagging {len(admitted):,} interpretive title(s) in "
        f"{len(batches):,} batches, "
        f"{concurrency} at a time"
    )

    async def run_batch(index: int, batch: list) -> None:
        """One batch, on its own session.

        Sessions are per-batch rather than shared because concurrent batches
        interleaving statements on one session is not safe, and because the
        atomic swap's guarantee is per-title inside one transaction -- which
        holds independently in each session.
        """
        nonlocal halted, completed

        async with semaphore:
            # Re-checked inside the semaphore: with N in flight, the stop or the
            # ceiling can land after this batch was queued but before it starts,
            # and spending on it anyway is exactly what the guard is for.
            if halted or stats["halted"] or (should_stop and should_stop()):
                return
            # Against the RUN total, not this call's. This also protects future
            # callers that split a single operator job across several calls.
            run_spend = spent_usd + stats["cost_usd"]
            if run_spend >= settings.retag_cost_ceiling_usd:
                if not halted:
                    halted = True
                    reason = (
                        f"Halted: accounted spend ${run_spend:.2f} reached "
                        f"the ${settings.retag_cost_ceiling_usd:.2f} ceiling"
                    )
                    _latch_halt(stats, reason)
                    logger.error(reason)
                return

            await _tag_one_batch(
                index=index,
                batch=batch,
                batches_total=len(batches),
                engine=engine,
                engine_provider=engine_provider,
                engine_model=engine_model,
                version=version,
                aliases=aliases,
                tag_ids=tag_ids,
                replace_stale=replace_stale,
                replace_all=replace_all,
                stats=stats,
                session_factory=session_factory,
            )

        completed += 1
        # No awaits in this block: one completed coroutine latches the breaker
        # before another can observe it, so four in-flight batches cannot emit
        # the same halt four times (as the production pilot did).
        if not halted:
            breaker_reason = _circuit_breaker_reason(stats)
            if breaker_reason:
                halted = True
                _latch_halt(stats, breaker_reason)
                logger.error(breaker_reason)
        if on_progress:
            on_progress(dict(stats, batch=completed, batches=len(batches)))
        if completed % 10 == 0 or completed == len(batches):
            logger.info(
                f"{completed}/{len(batches)} batches — {stats['processed']:,} "
                f"tagged, {stats['tags_written']:,} tags, ${stats['cost_usd']:.2f}"
            )
        if check_pause:
            await _maybe_await(check_pause())

    await asyncio.gather(*(run_batch(i, b) for i, b in enumerate(batches, start=1)))

    if (
        stats["missing_results"]
        and not stats["processed"]
        and stats["missing_results"] == stats["requeued"]
    ):
        _latch_halt(
            stats,
            f"Halted: the model omitted all {stats['missing_results']} requested "
            "titles from its response",
        )
    if stats["failed"] and not stats["processed"]:
        logger.error(
            "Every batch failed. First error: " + (stats["first_error"] or "unknown")
        )
    stats.pop("_pricing_provider", None)
    stats.pop("_pricing_model", None)
    return stats


async def _tag_one_batch(
    *,
    index: int,
    batch: list,
    batches_total: int,
    engine: Any,
    engine_provider: str,
    engine_model: str,
    version: str,
    aliases: dict,
    tag_ids: dict,
    replace_stale: bool,
    replace_all: bool,
    stats: dict[str, Any],
    session_factory: Callable[[], Any],
) -> None:
    """Tag one batch and write it, inside its own database session."""
    # Imported here to keep taxonomy/database-only tools from constructing the
    # shared network client merely by importing this worker.
    from app.tagging_provider import TaggingCallError, TaggingContentBlocked

    with session_factory() as db:
        try:
            prompt, vocab = build_prompt(batch)
            vocab_names = [tag.name for tag in vocab]
            # The provider schema needs the batch union, but validation must be
            # title-scoped. Validating against ``vocab_names`` let a title keep
            # any tag shortlisted for any of its 19 neighbours -- the source of
            # widespread, semantically impossible assignments in the audit.
            allowed_by_id = {
                int(item["tmdb_id"]): shortlist_names(item) for item in batch
            }
            index_by_name = {name: index for index, name in enumerate(vocab_names)}
            request_constraints: dict[str, Any] = {
                "allowed_tags": vocab_names,
                "expected_ids": [int(item["tmdb_id"]) for item in batch],
            }
            if engine_provider == "openai":
                request_constraints["allowed_indexes_by_id"] = {
                    tmdb_id: [
                        index_by_name[name]
                        for name in allowed_names
                        if name in index_by_name
                    ]
                    for tmdb_id, allowed_names in allowed_by_id.items()
                }
            try:
                tagged, usage = await engine.tag_items_raw(
                    prompt, **request_constraints
                )
            except Exception as exc:  # model nuance is optional in taxonomy v5
                stats["api_calls"] += 1
                error_usage = (
                    exc.usage
                    if isinstance(exc, (TaggingContentBlocked, TaggingCallError))
                    else None
                )
                _account_usage(stats, error_usage)
                stats["provider_failures"] += len(batch)
                if isinstance(exc, TaggingContentBlocked):
                    stats["blocked"] += len(batch)
                if not stats["first_error"]:
                    stats["first_error"] = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    f"batch {index}/{batches_total}: optional interpretive "
                    f"tagging failed for {len(batch)} title(s); publishing "
                    f"deterministic tags: {exc}"
                )
                tagged, usage = [], {}
            else:
                stats["api_calls"] += 1

                # Usage comes back with the items rather than off
                # engine.last_usage: the engine is a singleton and up to
                # gemini_concurrency batches call it at once.
                _account_usage(stats, usage)

            by_id: dict[int, dict[str, Any]] = {}
            for response_item in tagged:
                if not isinstance(response_item, dict):
                    continue
                try:
                    response_id = int(response_item.get("tmdb_id"))
                except (TypeError, ValueError):
                    continue
                response_item["tmdb_id"] = response_id
                by_id[response_id] = response_item

            # Provider omissions are measured, but never retried. The factual
            # set is already complete, so another paid call can add nuance but
            # cannot make the title valid or invalid.
            missing_batch = [
                item for item in batch if int(item["tmdb_id"]) not in by_id
            ]
            stats["model_omissions"] += len(missing_batch)

            def validate_payload(item: dict, payload: dict) -> Any:
                tmdb_id = int(item["tmdb_id"])
                return validate_item_tags(
                    tmdb_id,
                    item["media_type"],
                    payload.get("tags") or [],
                    shortlist_names=allowed_by_id.get(tmdb_id, []),
                    db_aliases=aliases,
                    derived=item.get("_deterministic_tags") or derive_tags(item),
                    min_confidence=settings.tag_min_confidence,
                    max_per_item=settings.tag_max_per_item,
                )

            validated_by_id = {
                int(item["tmdb_id"]): validate_payload(
                    item, by_id.get(int(item["tmdb_id"])) or {}
                )
                for item in batch
            }

            rows: list[dict[str, Any]] = []
            unresolved: list[tuple[str, str, int]] = []
            unresolved_tag_count = 0
            extra_observation_count = 0
            now = utc_now()

            for item in batch:
                tmdb_id = item["tmdb_id"]
                media_type = item["media_type"]
                payload = by_id.get(tmdb_id) or {}
                result = validated_by_id[int(tmdb_id)]
                for entry in result.unresolved:
                    unresolved.append((entry.raw, media_type, tmdb_id))
                    if len(stats["observation_samples"]) < 50:
                        stats["observation_samples"].append(
                            {
                                "tmdb_id": int(tmdb_id),
                                "media_type": media_type,
                                "kind": "unresolved_tag",
                                "raw": entry.raw[:160],
                            }
                        )
                unresolved_tag_count += len(result.unresolved)
                extras = payload.get("extra") or []
                for extra in extras:
                    unresolved.append((str(extra), media_type, tmdb_id))
                    if len(stats["observation_samples"]) < 50:
                        stats["observation_samples"].append(
                            {
                                "tmdb_id": int(tmdb_id),
                                "media_type": media_type,
                                "kind": "extra",
                                "raw": str(extra)[:160],
                            }
                        )
                extra_observation_count += len(extras)

                if result.requeue:
                    stats["requeued"] += 1
                    stats["terminal_skips"] += 1
                    if payload:
                        stats["low_tag_results"] += 1
                        attempt_status = "too_few_valid_tags"
                        if len(stats["low_tag_diagnostics"]) < 50:
                            rejected_reasons: dict[str, int] = {}
                            for rejected in result.rejected:
                                reason = rejected.reason or "rejected"
                                rejected_reasons[reason] = (
                                    rejected_reasons.get(reason, 0) + 1
                                )
                            stats["low_tag_diagnostics"].append(
                                {
                                    "tmdb_id": int(tmdb_id),
                                    "media_type": media_type,
                                    "title": str(item.get("title") or "")[:160],
                                    "returned": len(payload.get("tags") or []),
                                    "surviving_model_tags": sum(
                                        entry.method != "derived"
                                        for entry in result.accepted
                                    ),
                                    "rejected_reasons": rejected_reasons,
                                    "unresolved": len(result.unresolved),
                                }
                            )
                    else:
                        stats["missing_results"] += 1
                        attempt_status = "missing_model_result"
                    _mark_terminal_attempt(
                        db,
                        tmdb_id=tmdb_id,
                        media_type=media_type,
                        version=version,
                        status=attempt_status,
                    )
                    continue

                # A successful forced run supersedes any prior terminal
                # disposition at this version. Keeping a stale marker would
                # incorrectly suppress the title if its tags were later
                # removed by repair or operator action.
                if item.get("_tag_attempt_version") is not None:
                    db.query(MediaMetadata).filter(
                        MediaMetadata.tmdb_id == tmdb_id,
                        MediaMetadata.media_type == media_type,
                    ).update(
                        {
                            MediaMetadata.tag_attempt_version: None,
                            MediaMetadata.tag_attempt_status: None,
                            MediaMetadata.tag_attempted_at: None,
                        },
                        synchronize_session=False,
                    )

                if replace_stale:
                    # Atomic swap: this title's stale rows go, its new rows land,
                    # all inside the caller's transaction.
                    conditions = [
                        MovieTag.tmdb_id == tmdb_id,
                        MovieTag.media_type == media_type,
                    ]
                    if not replace_all:
                        # Normal path: only rows from a different version go, so
                        # re-running is cheap and idempotent.
                        conditions.append(
                            (MovieTag.tagger_version.is_(None))
                            | (MovieTag.tagger_version != version)
                        )
                    # replace_all: the title is already AT this version and is
                    # being re-tagged because its inputs changed. Keeping the
                    # version-based predicate would leave superseded tags
                    # behind, since the upsert only overwrites rows whose tag_id
                    # recurs -- a tag the model no longer assigns would survive
                    # forever.
                    db.query(MovieTag).filter(*conditions).delete(
                        synchronize_session=False
                    )

                for accepted in result.accepted:
                    if accepted.tag is None:
                        continue
                    name = accepted.tag.name
                    tag_id = tag_ids.get(name)
                    if tag_id is None:
                        continue
                    derived = accepted.method == "derived"
                    rows.append(
                        {
                            "tmdb_id": tmdb_id,
                            "media_type": media_type,
                            "tag_id": tag_id,
                            "confidence": accepted.confidence,
                            "weight": None,
                            "weight_norm": None,
                            "tagged_at": now,
                            "tagger_version": version,
                            "source": "derived" if derived else engine_provider,
                            "model": None if derived else engine_model[:40],
                        }
                    )
                stats["processed"] += 1

            upsert_movie_tags(db, rows)
            record_observations(db, unresolved)
            db.commit()

            stats["tags_written"] += len(rows)
            stats["off_vocabulary"] += len(unresolved)
            stats["unresolved_tags"] += unresolved_tag_count
            stats["extra_observations"] += extra_observation_count

        except Exception as exc:  # noqa: BLE001 - one batch must not end the run
            db.rollback()
            stats["failed"] += len(batch)
            if not stats["first_error"]:
                stats["first_error"] = f"{type(exc).__name__}: {exc}"
            logger.error(f"batch {index}/{batches_total} failed: {exc}")


def _account_usage(stats: dict[str, Any], usage: Any) -> None:
    """Fold one call's token usage into the run totals and recompute spend."""
    usage = usage or {}
    stats["input_tokens"] += usage.get("prompt_token_count", 0)
    stats["output_tokens"] += usage.get("candidates_token_count", 0) + usage.get(
        "thoughts_token_count", 0
    )
    if usage.get("estimated"):
        stats["estimated_usage"] = True
    stats["api_attempts"] += usage.get("attempt_count", 1)
    stats["cached_tokens"] += usage.get("cached_content_token_count", 0)
    stats["thinking_tokens"] += usage.get("thoughts_token_count", 0)
    stats["cost_usd"] = estimate_cost(
        stats["input_tokens"],
        stats["output_tokens"],
        model=stats.get("_pricing_model"),
        provider=stats.get("_pricing_provider"),
    )


async def _maybe_await(value: Any) -> Any:
    if asyncio.iscoroutine(value):
        return await value
    return value


def build_features(row: Any) -> dict[str, Any]:
    """Turn a MediaMetadata row into the shape the prompt and taxonomy expect."""
    return {
        "tmdb_id": row.tmdb_id,
        "media_type": row.media_type,
        "title": row.title,
        "overview": row.overview or "",
        "genres": row.genres if isinstance(row.genres, list) else [],
        "release_date": row.release_date or "",
        "original_language": row.original_language,
        "tag_facts": row.tag_facts if isinstance(row.tag_facts, dict) else {},
        "mdb_facts": row.mdb_facts if isinstance(row.mdb_facts, dict) else {},
        # derive_tags reads these directly.
        "countries": (
            (row.tag_facts or {}).get("countries")
            if isinstance(row.tag_facts, dict)
            else None
        ),
        "runtime": (
            (row.tag_facts or {}).get("runtime")
            if isinstance(row.tag_facts, dict)
            else None
        ),
        "number_of_seasons": row.number_of_seasons,
        "number_of_episodes": row.number_of_episodes,
        # Control-plane state, ignored by prompt rendering and derivation. It
        # avoids an extra UPDATE for every normal title while still letting a
        # successful forced run clear its prior terminal disposition.
        "_tag_attempt_version": row.tag_attempt_version,
    }


def shuffle_within_tiers(
    ranked: Sequence[tuple[Any, float]],
    rng: Any,
    *,
    precision: int = 2,
) -> list[Any]:
    """Shuffle equally-scored items so rows vary day to day without churning.

    ``ranked`` is ``[(item, score)]``, best first. Items whose score matches to
    ``precision`` decimal places form a tier and are shuffled among themselves;
    tiers keep their relative order. So the ordering stays honest -- a better
    match never drops below a worse one -- while ties break differently each
    day, which is what stops a row looking frozen.

    There were four hand-rolled copies of this in ``personalized_sync`` (the
    similar-by-tags, recommendations, taste and hidden-gems paths), each
    tiering on a different field and one of them subtly different. They tiered
    on integer tag counts because that was the only score available; now that
    scores are continuous, rounding is what defines a tier.
    """
    out: list[Any] = []
    tier: list[Any] = []
    tier_key: float | None = None
    for item, score in ranked:
        key = round(float(score or 0.0), precision)
        if tier_key is None or key == tier_key:
            tier.append(item)
            tier_key = key
            continue
        rng.shuffle(tier)
        out.extend(tier)
        tier = [item]
        tier_key = key
    rng.shuffle(tier)
    out.extend(tier)
    return out
