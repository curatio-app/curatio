"""
Configuration management using Pydantic Settings.

Loads configuration from environment variables and .env file.
"""

import sys
from typing import Optional

from loguru import logger
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Settings whose range has to hold even when they are written at runtime.
#:
#: The admin dashboard applies overrides with ``object.__setattr__``, which is
#: the only way to mutate this frozen model -- and which bypasses every
#: validator on it. For most settings that is merely untidy. For these it is
#: not: a quality floor of 99 empties every row in the product, and the failure
#: is silent, because the query simply matches nothing. One table, read by both
#: the import-time validators below and ``admin._apply_setting``, so the two
#: paths cannot drift.
NUMERIC_BOUNDS: dict[str, tuple[float, float]] = {
    # Recommendation rows are intentionally premium-only. The production pilot
    # was run with a persisted 6.2 override and admitted 16.5% of its titles
    # below the repository's intended 6.8 floor. Runtime overrides are clamped
    # here as well as import-time values, so an old admin_settings row cannot
    # silently reopen the C-tier tail after deployment.
    "catalog_min_quality": (6.8, 10.0),
    "catalog_niche_min_quality": (0.0, 10.0),
    # Above 0: at 0 the multiplier can reach zero, and quality stops
    # *modulating* relevance and starts *replacing* it -- inverting the one
    # property the blended score exists to guarantee.
    "catalog_quality_weight": (0.05, 1.0),
    "catalog_min_vote_count": (0, 1_000_000),
    "catalog_niche_min_vote_count": (0, 1_000_000),
    "catalog_niche_min_age_days": (0, 36_500),
    "catalog_min_rating_sources": (0, 10),
    "retag_cost_ceiling_usd": (0.1, 1_000.0),
    "library_tagging_monthly_cap_usd": (0.0, 1_000.0),
    "synth_editor_cost_ceiling_usd": (0.01, 5.0),
}


def _check_bounds(name: str, value):
    low, high = NUMERIC_BOUNDS[name]
    if not (low <= value <= high):
        raise ValueError(f"{name} must be between {low} and {high}")
    return value


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # TMDB API
    tmdb_api_key: str = Field(..., description="TMDB API key")

    # Tagging provider. Keys may also be stored from Admin Center, so none of
    # the provider credentials is required merely to boot the application.
    tagging_provider: str = Field(
        "gemini", description="AI provider used for closed-vocabulary tagging"
    )

    # Google Gemini API
    gemini_api_key: str = Field("", description="Google Gemini API key")
    # Google retires Gemini model IDs on a published schedule and an ID that has
    # passed its shutdown date returns 404 on every call. gemini-2.0-flash shut
    # down 2026-06-01 and silently broke all tagging until the failure was traced
    # back to it; gemini-2.5-flash follows on 2026-10-16. Run
    # `python workers/diagnose_tagging.py` to list the models your key can
    # actually reach and verify the configured one still answers.
    gemini_model: str = Field(
        "gemini-2.5-flash-lite", description="Gemini model to use"
    )

    # OpenAI API (a ChatGPT subscription does not include API usage).
    openai_api_key: str = Field("", description="OpenAI API key")
    openai_model: str = Field(
        "gpt-5.4-nano", description="OpenAI model to use for tagging"
    )

    # Anthropic API (a Claude/Claude Code subscription does not include API usage).
    anthropic_api_key: str = Field("", description="Anthropic API key")
    anthropic_model: str = Field(
        "claude-haiku-4-5-20251001",
        description="Anthropic Claude model to use for tagging",
    )

    # A local model behind an OpenAI-compatible API (Ollama, LM Studio, ...).
    local_base_url: str = Field(
        "",
        description="OpenAI-compatible base URL, e.g. http://localhost:11434/v1",
    )
    local_api_key: str = Field("", description="API key for the local endpoint, if any")
    local_model: str = Field("", description="Local model name, e.g. qwen2.5:14b")

    # --- tagging (taxonomy 2.0) ---
    tagging_batch_size: int = Field(
        20,
        description="Titles per Gemini tagging request. The worker also caps legacy "
        "environment overrides at the verified safe maximum.",
    )
    gemini_concurrency: int = Field(
        4,
        description="Tagging batches in flight at once. Batches used to run "
        "strictly one at a time with a fixed sleep between them, which made a "
        "full rebuild many hours of mostly waiting. Lower this if the API "
        "returns 429s faster than the backoff absorbs them; 1 restores the old "
        "serial behaviour.",
    )
    gemini_temperature: float = Field(
        0.2,
        description="Sampling temperature for tagging. This is closed-vocabulary "
        "classification, not generation, so it wants to be near-deterministic; "
        "the SDK default of ~1.0 was costing precision for nothing.",
    )
    tag_min_confidence: float = Field(
        0.60,
        description=(
            "Drop tags the model scored below this. The production pilot showed "
            "that 0.50-0.59 assignments were disproportionately speculative."
        ),
    )
    tag_max_per_item: int = Field(
        22,
        description=(
            "Hard cap on tags stored per title, including deterministic facts. "
            "The validator also caps model judgements at 6 and applies per-facet limits."
        ),
    )
    tag_max_requeue_rate: float = Field(
        0.10,
        description="Steady-state halt threshold for safely skipped malformed/near-empty responses",
    )
    tag_max_failure_rate: float = Field(
        0.02,
        description="Steady-state halt threshold for request or validation failures",
    )
    tag_circuit_breaker_min_items: int = Field(
        1000,
        description="Attempted titles required before steady-state quality breakers apply",
    )
    tag_catastrophic_rate: float = Field(
        0.20,
        description="Early halt threshold for either requeues or hard failures",
    )
    tag_catastrophic_min_items: int = Field(
        200,
        description="Attempted titles required before the catastrophic breaker applies",
    )
    gemini_max_attempts: int = Field(
        8,
        description="Maximum attempts during a transient Gemini outage. Shared backoff "
        "keeps concurrent callers from multiplying requests; each attempt may bill.",
    )

    # --- stream provider health ---
    provider_health_failure_threshold: int = Field(
        2,
        description="Reported failures for one stream provider within the window "
        "before its streams are deprioritized in results.",
    )
    provider_health_window_seconds: int = Field(
        900,
        description="Rolling window (seconds) over which provider failures are "
        "counted; failures older than this age out and stop counting.",
    )

    # Cost accounting. Defaults match gemini-2.5-flash-lite; check your billing
    # console, and remember batch mode is typically half of these.
    gemini_input_cost_per_mtok: float = Field(
        0.10, description="USD per 1M input tokens, for job cost accounting"
    )
    gemini_output_cost_per_mtok: float = Field(
        0.40, description="USD per 1M output tokens, for job cost accounting"
    )
    retag_cost_ceiling_usd: float = Field(
        20.0,
        description="Halt a re-tag once its measured or reserved spend reaches this",
    )
    # Library-first tagging: only titles on the connected servers (and saved
    # to a Watchlist) are AI-tagged unless this is turned on.
    tag_catalog_beyond_library: bool = Field(
        False,
        description="Also AI-tag new TMDB releases nobody owns (nightly job)",
    )
    library_tagging_monthly_cap_usd: float = Field(
        0.0,
        description="Stop AI-tagging owned titles for the month at this spend (0 = no cap)",
    )
    # Where installs download Curatio's initial tag pack (a manifest URL).
    tag_pack_url: str = Field(
        "https://github.com/curatio-app/curatio/releases/download/tag-pack/manifest.json",
        description="Manifest URL of the initial tag pack (empty = no download)",
    )

    # --- TMDB enrichment ---
    tmdb_enrich_concurrency: int = Field(
        12, description="Concurrent TMDB detail requests during enrichment"
    )
    tmdb_enrich_rps: float = Field(
        20.0, description="TMDB requests per second ceiling during enrichment"
    )
    enrich_batch_size: int = Field(500, description="Rows per enrichment commit")
    raw_data_prune: bool = Field(
        True,
        description="Strip images/videos and trim credits from stored TMDB "
        "payloads. Detail payloads run 50-250KB and only seasons[].episode_count "
        "is ever read back; without this, enriching the library would add ~17GB.",
    )

    # --- MDBList (optional; the API key lives in admin_settings) ---
    mdblist_enabled: bool = Field(True, description="Enable MDBList integration")
    mdblist_concurrency: int = Field(
        8,
        description="Concurrent MDBList requests.",
    )
    mdblist_rps: float = Field(
        5.0,
        description="MDBList requests per second. Concurrency alone bounds only "
        "how many requests are in flight, not the rate: unpaced, 8-wide "
        "enrichment tripped a 429 after ~8,000 requests against a 500,000/day "
        "key and ended a full-library pass at 2.4%. At 5/s a 354k library is "
        "~35,400 requests (batch size 10) in about two hours, which is 7% of a "
        "500,000/day quota. Lower it if 429s persist; the client backs off and "
        "retries either way.",
    )
    mdblist_batch_size: int = Field(
        10,
        description="Titles per MDBList batch lookup. Left at this default the "
        "batch size is taken from the key itself (/user reports 100 for a "
        "supporter, 10 otherwise), so a supporter key gets ~3,500 requests for "
        "a 354k library rather than ~35,000 with no configuration. Set it to "
        "anything else to override deliberately; the value is still clamped to "
        "what the key accepts, so an over-large setting degrades rather than "
        "fails.",
    )
    mdblist_daily_reserve_pct: float = Field(
        0.05, description="Fraction of the discovered daily quota held back"
    )
    mdblist_refresh_days: int = Field(
        0,
        description="Re-fetch per-title MDBList facts older than N days. 0 "
        "disables it: keywords/certification/age ratings do not drift, and the "
        "one field that does (ratings) is refreshed by the cheap bulk pass.",
    )
    mdblist_review_chars: int = Field(
        400, description="Max characters kept per stored review"
    )
    mdblist_reviews_per_item: int = Field(2, description="Max reviews stored per title")
    mdblist_min_rating_sources: int = Field(
        2, description="Rating sources required before consensus overrides TMDB"
    )

    # Master Password
    master_password: str = Field(..., description="Admin fallback login password")

    # Database
    database_url: str = Field(..., description="PostgreSQL connection string")
    db_pool_size: int = Field(
        16,
        description="Request-path connection pool size; also sizes the default "
        "executor that request handlers offload synchronous DB work to",
    )
    db_max_overflow: int = Field(8, description="Max overflow connections")
    db_pool_timeout: int = Field(
        15,
        description="Seconds to wait for a pooled connection before failing fast "
        "(default SQLAlchemy is 30)",
    )
    db_request_lock_timeout_ms: int = Field(
        5000,
        description="Postgres lock_timeout for request-path connections (0 = off)",
    )
    db_request_statement_timeout_ms: int = Field(
        0,
        description="Postgres statement_timeout for request-path connections "
        "(0 = off)",
    )
    db_request_idle_in_transaction_ms: int = Field(
        300000,
        description="Postgres idle_in_transaction_session_timeout for "
        "request-path connections (0 = off)",
    )
    db_job_pool_size: int = Field(
        6, description="Connection pool size for background jobs"
    )
    db_job_max_overflow: int = Field(
        6, description="Max overflow connections for background jobs"
    )
    db_job_idle_in_transaction_ms: int = Field(
        1800000,
        description="Postgres idle_in_transaction_session_timeout for "
        "background-job connections (0 = off)",
    )
    meta_cache_max_entries: int = Field(
        1024,
        description="In-process detail/meta cache entries (up to four per title; "
        "large series payloads can be ~1 MB each)",
    )
    job_executor_workers: int = Field(
        8,
        description="Threads for background jobs, kept apart from the request "
        "executor (catalog syncs, daily update, home-feed builders, warmers)",
    )

    # Application
    base_url: str = Field(..., description="Public base URL of this server")
    webauthn_extra_origins: str = Field(
        default="",
        description=(
            "Comma-separated extra browser origins (scheme://host[:port]) allowed "
            "for passkey sign-in besides BASE_URL, e.g. a second public hostname"
        ),
    )
    app_name: str = Field("Curatio", description="Product name shown to users")
    mobile_redirect_schemes: str = Field(
        "curatio",
        description="Comma-separated native app URL schemes allowed for mobile auth callbacks",
    )

    # Catalog Settings
    catalog_size: int = Field(200, description="Number of items per catalog")
    catalog_page_size: int = Field(100, description="Items returned per catalog page")
    catalog_shuffle_hours: int = Field(
        3, description="Reshuffle catalog order every N hours (0 to disable)"
    )
    universal_catalog_refresh_hours: int = Field(
        24, description="Hours between universal catalog refreshes"
    )
    personalized_catalog_refresh_hours: int = Field(
        24, description="Hours between personalized catalog refreshes"
    )
    coming_soon_tv_refresh_limit: int = Field(
        300,
        description=(
            "Max series re-checked for their next-episode air date per daily update "
            "(0 disables the refresh)"
        ),
    )
    personalized_catalog_count: int = Field(
        19, description="Maximum stable per-profile catalog slots"
    )
    universal_catalog_count: int = Field(
        40,
        description="Legacy curated catalog count (compatibility documentation only)",
    )
    synth_excluded_tags: str = Field(
        "",
        description=(
            "Comma-separated tag names that may not anchor a GENERATED catalog "
            "row. Empty by default: which shelves an install wants is an "
            "editorial decision belonging to its operator, not something this "
            "code should presume. Set it in the admin console (Tagging tab), "
            "which stores it in admin_settings and applies it live. Scope is "
            "narrow -- excluded tags stay in the taxonomy, stay applied to "
            "titles, and those titles still appear in every other row they "
            "match and in search."
        ),
    )
    synth_excluded_words: str = Field(
        "",
        description=(
            "Comma-separated words that may not appear in a generated row NAME, "
            "case-insensitive. A safety net behind synth_excluded_tags: a "
            "template or a future taxonomy addition could reintroduce a word "
            "even when its tag is excluded. Empty by default, same reasoning."
        ),
    )
    synth_rotation_days: int = Field(
        7,
        description=(
            "How often the generated rows on show are rotated. Synthesis "
            "produces more rows than a home screen can carry, so the "
            "surplus would otherwise never be seen. 0 disables rotation and "
            "pins the highest-lift rows permanently."
        ),
    )
    synth_editor_enabled: bool = Field(
        False,
        description=(
            "Run the bounded AI editorial gate for changed synthesized shelves. "
            "When enabled, a failed/incomplete review leaves the currently "
            "published snapshot untouched."
        ),
    )
    synth_editor_provider: str = Field(
        "",
        description=(
            "Independent shelf-editor provider (gemini, anthropic, or openai). "
            "Blank inherits TAGGING_PROVIDER without changing the tagger version."
        ),
    )
    synth_editor_model: str = Field(
        "",
        description=(
            "Independent shelf-editor model. Blank inherits the selected "
            "provider's configured tagging model."
        ),
    )
    synth_editor_cost_ceiling_usd: float = Field(
        0.25,
        ge=0.01,
        le=5.0,
        description="Hard USD ceiling for one synthesized-shelf editorial pass",
    )

    # Logging
    log_level: str = Field("INFO", description="Logging level")
    debug: bool = Field(False, description="Enable debug mode")

    # Rate Limiting
    rate_limit_per_minute: int = Field(60, description="Requests per minute per user")
    trusted_proxy_count: int = Field(
        0,
        description=(
            "Number of trusted reverse-proxy hops in front of the app. 0 (default) "
            "trusts no proxy headers and uses the real socket peer for client-IP "
            "derivation and rate limiting. Set to 1 when running behind a single "
            "trusted proxy (e.g. the bundled nginx) so the real client IP is read "
            "safely from the right of X-Forwarded-For instead of the spoofable "
            "left-most value."
        ),
    )

    # Security
    secret_key: str = Field(..., description="Secret key for JWT tokens")
    token_expiration_days: int = Field(30, description="JWT token expiration in days")
    force_secure_cookies: bool = Field(
        True, description="Always mark auth cookies Secure"
    )

    security_csp_report_only: bool = Field(
        False, description="Serve Content-Security-Policy in report-only mode"
    )
    security_encryption_keys: str = Field(
        "",
        description=(
            "Comma-separated base secrets used to derive at-rest encryption keys. "
            "First key is active; remaining keys are accepted for decryption "
            "during rotation."
        ),
    )
    security_csp: str = Field(
        "default-src 'self'; base-uri 'self'; frame-ancestors 'none'; object-src 'none'; img-src 'self' https: data:; style-src 'self' 'unsafe-inline' https:; font-src 'self' data: https:; script-src 'self' https://cdn.jsdelivr.net https://esm.sh https://cdnjs.cloudflare.com; connect-src 'self' https: wss:; media-src 'self' https: blob:; worker-src 'self' blob: https://cdnjs.cloudflare.com",
        description="Content Security Policy for HTML routes",
    )

    # Feature Flags
    enable_universal_catalogs: bool = Field(
        True,
        description=(
            "Serve the shared synthesized rows. Legacy curated rows remain "
            "inactive compatibility records only. Documented in the README and "
            "offered as an admin toggle since the dashboard was written, but "
            "it was never a field on this class -- so the toggle wrote a row "
            "nothing read, and rendered OFF while the feature ran."
        ),
    )
    enable_personalized_catalogs: bool = Field(
        True,
        description=(
            "Serve the per-profile rows (Because You Watched, Recommended, "
            "taste and hidden-gem rows). Distinct from "
            "`enable_personalized_sync`, which controls whether the nightly "
            "job BUILDS them: turning this off hides rows that still exist, "
            "so turning it back on needs no rebuild."
        ),
    )
    enable_personalized_sync: bool = Field(
        True, description="Enable local personalized catalog sync"
    )
    shelf_pass_enabled: bool = Field(
        True,
        description=(
            "After the nightly sync, enrich and tag any title sitting on a "
            "shelf that carries no tags. The feed rows (Trending, New "
            "Releases, Popular, Coming Soon) are filled by a fetch the daily "
            "update never sees, so without this a trending title reaches a "
            "viewer with no tags -- no chips on its detail page, and More "
            "Like This silently falling back to TMDB."
        ),
    )
    shelf_pass_limit: int = Field(
        400,
        description=(
            "Titles the shelf pass may tag per run. Only binds after a "
            "taxonomy change or on a fresh install; a library already tagged "
            "finds a handful a night. At the measured $0.000071/title even the "
            "ceiling is under 3 cents."
        ),
    )

    # Content Filters (global defaults — users can override per-account)
    hide_foreign: bool = Field(False, description="Hide non-English content by default")
    hide_adult: bool = Field(False, description="Hide explicit/18+ content by default")
    hide_unreleased: bool = Field(
        False,
        description="Hide movies not yet available digitally (unreleased or in theaters)",
    )

    # Quality floor. Applies to row membership and to the paid re-tag corpus;
    # the optional per-run `min_votes` can narrow that corpus further.
    catalog_min_rating_sources: int = Field(
        5,
        description=(
            "How many independent rating sources (IMDb, Rotten Tomatoes, "
            "Metacritic, Trakt, Letterboxd...) must carry a title before it "
            "may appear in a catalog row. The most direct 'has anyone heard "
            "of this' signal in the schema: MDBList writes it per title, and "
            "it is 0 for precisely the obscure tail MDBList does not carry. "
            "A title with no MDBList data can still qualify through the TMDB "
            "vote fallback, so a key-less install does not empty every row. 0 disables "
            "the filter. Five sources is the production-audited default: the "
            "job 223 pilot retained every qualifying movie while cutting the "
            "under-100-vote TV tail from 111 titles to 32."
        ),
    )
    catalog_min_vote_count: int = Field(
        1000,
        description=(
            "TMDB vote floor for catalog membership, used as the FALLBACK arm "
            "of the floor: a title MDBList has never been asked about can "
            "still qualify on votes alone. It was 10, which is not a floor -- "
            "combined with the rating-source arm being skipped for every "
            "un-enriched title, effectively nothing was excluded. A "
            "per-category formula may still override it."
        ),
    )
    catalog_min_quality: float = Field(
        6.8,
        description=(
            "Minimum 0-10 quality score for catalog membership, measured on "
            "the same `quality_expr()` the rows are RANKED by -- MDBList's "
            "votes-weighted consensus where it exists, a Bayesian-smoothed "
            "TMDB average otherwise. This is the only rating floor in the "
            "product: before it, quality could nudge a title's position but "
            "never keep it out, so a widely-rated 4.8/10 passed every gate. "
            "0 disables it."
        ),
    )
    catalog_quality_weight: float = Field(
        0.30,
        description=(
            "Lower bound of the quality/buzz multiplier applied to relevance "
            "when ranking a row (see `_blended_score_expr`). The multiplier "
            "spans [this, 1.0], so a smaller value gives quality more "
            "authority over ordering: at 0.55 it could not reorder titles "
            "whose relevance differed by more than ~1.8x. Relevance is still "
            "the primary term at any value -- popularity can modulate a "
            "well-matched title, never float a badly-matched one. 1.0 turns "
            "the modulation off entirely."
        ),
    )
    catalog_niche_min_quality: float = Field(
        7.4,
        description="Higher quality threshold that lets older, less-voted niche titles qualify",
    )
    catalog_niche_min_vote_count: int = Field(
        250,
        description="Vote floor for the high-quality niche exception",
    )
    catalog_niche_min_age_days: int = Field(
        180,
        description="Niche exception applies only after enough time has passed to collect votes",
    )

    # Performance
    workers: int = Field(4, description="Number of Uvicorn workers")
    cache_ttl: int = Field(3600, description="Cache TTL in seconds")
    fingerprint_ffmpeg_threads: int = Field(
        1,
        ge=1,
        le=8,
        description=(
            "Decoder threads available to each background fingerprint FFmpeg pass. "
            "Fingerprint jobs are latency-insensitive and share the web container, so "
            "one thread prevents a single media decode from saturating the server."
        ),
    )

    # Skip Intro / end credits. The two type switches decide what the apps are
    # offered at all; the three source switches decide where times come from.
    skip_intro_enabled: bool = Field(True, description="Offer Skip Intro in the apps.")
    skip_credits_enabled: bool = Field(
        True, description="Offer end-credits times (the Next Episode prompt)."
    )
    introdb_enabled: bool = Field(
        True, description="Look up intro and credits times in IntroDB."
    )
    theintrodb_enabled: bool = Field(
        True, description="Look up intro and credits times in TheIntroDB."
    )
    fingerprint_segments_enabled: bool = Field(
        True,
        description=(
            "Queue fingerprint jobs on playback and serve the learner's markers "
            "when the community databases have nothing."
        ),
    )
    theintrodb_api_key: Optional[str] = Field(
        None,
        description="Optional TheIntroDB API key; raises its read rate limits.",
    )

    # Native detail snapshots are the latency-critical read model used by the
    # Apple clients. Keep their warmer independent from the paid daily tagging
    # scheduler so a deployment with DAILY_UPDATE_ENABLED=false still prepares
    # the titles users can currently select.
    detail_snapshot_warm_enabled: bool = Field(
        True, description="Continuously warm active native detail snapshots"
    )
    detail_snapshot_warm_interval_seconds: int = Field(
        3600,
        ge=300,
        description="Seconds between resumable active-inventory detail warm passes",
    )
    detail_snapshot_warm_startup_delay_seconds: int = Field(
        10,
        ge=0,
        le=300,
        description="Delay before the first in-process detail warm pass",
    )
    detail_snapshot_warm_concurrency: int = Field(
        2,
        ge=1,
        le=4,
        description="Concurrent background detail builds; foreground capacity is separate",
    )
    detail_snapshot_warm_limit: int = Field(
        2000,
        ge=0,
        description="Maximum prioritized titles per pass; 0 means unlimited",
    )
    detail_dynamic_overlay_ttl_seconds: int = Field(
        24 * 60 * 60,
        ge=3600,
        description=(
            "Seconds a persisted tags/ratings/similarity overlay remains fresh. "
            "Persisted freshness prevents process restarts from recomputing every title."
        ),
    )
    detail_dynamic_refresh_concurrency: int = Field(
        1,
        ge=1,
        le=4,
        description=(
            "Concurrent background detail-overlay database refreshes. Similarity "
            "uses a large movie_tags self-join, so the safe default is one."
        ),
    )

    # Playback byte proxy (same-origin media passthrough for web playback)
    playback_proxy_max_streams: int = Field(
        24,
        description="Max concurrent playback proxy streams before shedding "
        "further requests with a 503. Raise for deployments with many "
        "simultaneous Safari/native-proxy viewers.",
    )
    playback_proxy_chunk_bytes: int = Field(
        256 * 1024,
        description="Chunk size (bytes) used when relaying proxied media",
    )
    playback_remux_max_streams: int = Field(
        3,
        ge=1,
        le=16,
        description="Maximum concurrent H.264-to-HLS browser compatibility sessions",
    )
    playback_remux_idle_timeout: int = Field(
        90,
        ge=30,
        le=3600,
        description="Seconds without an HLS asset request before a compatibility session is reaped",
    )
    allow_server_transcode: bool = Field(
        False,
        description=(
            "Allow the server to fully transcode video (H.264, at most 1080p, "
            "HDR tone-mapped to SDR) for browsers that cannot decode a source. "
            "Off by default: transcoding is CPU-heavy on small hosts. When off, "
            "transcode-only sources fail over to the next source and the web "
            "player points viewers at the desktop app."
        ),
    )
    playback_transcode_max_streams: int = Field(
        1,
        ge=1,
        le=4,
        description="Maximum concurrent full video transcodes (counted within the remux cap)",
    )

    # Daily Update Scheduler
    daily_update_enabled: bool = Field(
        False, description="Enable daily content update scheduler"
    )
    daily_update_time: str = Field(
        "03:00", description="Time to run daily update in HH:MM format (UTC)"
    )
    daily_update_timeout: int = Field(
        1800,
        description="Overall deadline (seconds) for a single daily update run "
        "before it is killed",
    )
    gemini_request_timeout: int = Field(
        120,
        description="Per-request timeout (seconds) for AI tagging calls",
    )

    # Development
    skip_api_validation: bool = Field(
        False, description="Skip API key validation (testing only)"
    )

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, v):
        """Ensure base_url doesn't have trailing slash."""
        return v.rstrip("/")

    @field_validator("tagging_provider")
    @classmethod
    def validate_tagging_provider(cls, v):
        provider = str(v).strip().lower()
        if provider not in {"gemini", "anthropic", "openai", "local"}:
            raise ValueError(
                "tagging_provider must be one of: gemini, anthropic, openai, local"
            )
        return provider

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v):
        level = str(v).strip().upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(
                "log_level must be DEBUG, INFO, WARNING, ERROR, or CRITICAL"
            )
        return level

    @field_validator("synth_editor_provider")
    @classmethod
    def validate_synth_editor_provider(cls, v):
        provider = str(v or "").strip().lower()
        if provider and provider not in {"gemini", "anthropic", "openai"}:
            raise ValueError(
                "synth_editor_provider must be blank or one of: "
                "gemini, anthropic, openai"
            )
        return provider

    @field_validator("secret_key")
    @classmethod
    def validate_secret_key(cls, v):
        """Ensure secret key is long enough."""
        if len(v) < 32:
            raise ValueError("SECRET_KEY must be at least 32 characters long")
        return v

    @field_validator("daily_update_time")
    @classmethod
    def validate_daily_update_time(cls, v):
        """Fail fast on a malformed time instead of letting the scheduler task die
        silently at runtime (which would disable all scheduled updates)."""
        parts = str(v).strip().split(":")
        if len(parts) != 2:
            raise ValueError(f"daily_update_time must be HH:MM, got '{v}'")
        try:
            hour, minute = int(parts[0]), int(parts[1])
        except ValueError:
            raise ValueError(f"daily_update_time must be HH:MM, got '{v}'")
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError(f"daily_update_time out of range (HH:MM): '{v}'")
        return v

    @field_validator("catalog_size")
    @classmethod
    def validate_catalog_size(cls, v):
        """Ensure catalog size is reasonable."""
        if v < 10 or v > 500:
            raise ValueError("catalog_size must be between 10 and 500")
        return v

    @field_validator("catalog_min_quality", "catalog_niche_min_quality")
    @classmethod
    def validate_catalog_quality_floors(cls, v, info):
        if info.field_name == "catalog_min_quality":
            # Upgrade old .env files safely. Rejecting the former 6.2 value at
            # process startup would prevent the container from coming up at all;
            # runtime/database overrides are clamped by admin._bounded.
            v = max(v, NUMERIC_BOUNDS["catalog_min_quality"][0])
        return _check_bounds(info.field_name, v)

    @field_validator("tag_min_confidence")
    @classmethod
    def upgrade_former_tag_confidence_default(cls, v):
        # 0.45 was the former default, below the provider schema's own 0.50
        # minimum, so it filtered nothing. Upgrade that inert legacy value
        # while preserving deliberate operator choices.
        value = 0.60 if float(v) == 0.45 else float(v)
        if not 0.0 <= value <= 1.0:
            raise ValueError("tag_min_confidence must be between 0 and 1")
        return value

    @field_validator("catalog_min_rating_sources")
    @classmethod
    def upgrade_former_source_default(cls, v):
        # Existing .env files can persist the former default just as the admin
        # table can. Upgrade the two former defaults while preserving 0 (off)
        # and other explicit operator choices. A later Admin Center override is
        # applied after startup and remains under operator control.
        return 5 if v in {2, 4} else _check_bounds("catalog_min_rating_sources", v)

    @field_validator("catalog_quality_weight", "retag_cost_ceiling_usd")
    @classmethod
    def validate_bounded_float(cls, v, info):
        return _check_bounds(info.field_name, v)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


# Global settings instance
settings = Settings()  # type: ignore[call-arg]

# Loguru's built-in stderr sink defaults to DEBUG. Configure it centrally so
# web and standalone worker entry points both honor LOG_LEVEL; feature-specific
# sinks (such as the bounded Admin Center fingerprint feed) are added later and
# remain independent of this production stderr threshold.
logger.remove()
logger.add(
    sys.stderr,
    level=settings.log_level.upper(),
    backtrace=settings.debug,
    diagnose=settings.debug,
)


# Validation functions
def validate_api_keys():
    """Validate that all required API keys are configured."""
    if settings.skip_api_validation:
        return True

    errors = []

    # Check TMDB API key format
    if not settings.tmdb_api_key or settings.tmdb_api_key == "your_tmdb_api_key_here":
        errors.append("TMDB_API_KEY is not configured")

    # Provider credentials may be entered after boot in Admin Center. Validate
    # only a non-placeholder credential that is already present; the tagging
    # endpoints provide the provider-specific missing-key gate before a job.
    provider_keys = {
        "gemini": settings.gemini_api_key,
        "openai": settings.openai_api_key,
        "anthropic": settings.anthropic_api_key,
        # A local model usually needs no key.
        "local": settings.local_api_key,
    }
    if settings.tagging_provider not in provider_keys:
        errors.append(
            "TAGGING_PROVIDER must be one of: gemini, openai, anthropic, local"
        )

    # Check master password. It is a single-factor key to the whole admin area,
    # so it must be long enough to resist guessing.
    if (
        not settings.master_password
        or settings.master_password == "change_this_to_a_strong_password_123"
    ):
        errors.append("MASTER_PASSWORD is not configured")
    elif len(settings.master_password) < 16:
        errors.append("MASTER_PASSWORD must be at least 16 characters")

    # Reject the documented SECRET_KEY placeholder. SECRET_KEY also derives the
    # default at-rest encryption key (app/crypto.py), so shipping the well-known
    # example value exposes every stored secret (e.g. TOTP seeds).
    if (
        not settings.secret_key
        or settings.secret_key == "change_this_to_a_random_secret_key_min_32_chars"
    ):
        errors.append("SECRET_KEY is not configured (still the example placeholder)")

    if errors:
        raise ValueError(f"Configuration errors: {', '.join(errors)}")

    return True


def get_database_url() -> str:
    """Get database URL with proper formatting."""
    return settings.database_url
