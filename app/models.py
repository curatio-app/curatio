"""
Database models for Curatio.

This module defines all database tables using SQLAlchemy ORM.
"""

from app.time_utils import utc_now
from sqlalchemy import (
    Column,
    String,
    Integer,
    Float,
    Boolean,
    DateTime,
    JSON,
    ForeignKey,
    UniqueConstraint,
    Index,
    Text,
    BigInteger,
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class PlaybackPresence(Base):
    """Short-lived player presence, independent of durable watch progress."""

    __tablename__ = "playback_presence"
    session_id = Column(String(36), primary_key=True)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    device_id = Column(String(128), nullable=False)
    media_id = Column(String(160), nullable=False)
    sequence = Column(Integer, nullable=False)
    state = Column(String(16), nullable=False)
    details = Column(JSON, nullable=False)
    started_at = Column(DateTime, nullable=False)
    last_seen = Column(DateTime, nullable=False, index=True)


class PlaybackSourceRequest(Base):
    """Cross-worker source lookup activity; never evidence of playback."""

    __tablename__ = "playback_source_requests"
    request_id = Column(String(36), primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True)
    session_id = Column(String(36), nullable=True, index=True)
    state = Column(String(16), nullable=False)
    details = Column(JSON, nullable=False)
    started_at = Column(DateTime, nullable=False, index=True)
    finished_at = Column(DateTime, nullable=True)


class AdminSession(Base):
    """Persistent admin sessions (survives restarts)."""

    __tablename__ = "admin_sessions"

    token = Column(String(64), primary_key=True)
    user_id = Column(Integer, nullable=True)  # NULL for master-password sessions
    expires_at = Column(DateTime, nullable=False)


class Tag(Base):
    """Master list of all semantic tags used for categorization."""

    __tablename__ = "tags"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), unique=True, nullable=False, index=True)
    # The taxonomy facet (app/taxonomy.py FACETS). Pre-2.0 rows carried the old
    # 'genre'/'mood'/'era'/'region'/'plot'/'style'/'character' values; the first
    # sync_taxonomy() run rewrites them in place.
    category = Column(String(50), nullable=False, index=True)

    # Populated from TagDef.gloss. Never written before taxonomy 2.0.
    description = Column(Text, nullable=True)
    created_at = Column(DateTime, default=utc_now, nullable=False)

    # --- taxonomy 2.0 ---
    display = Column(Boolean, default=True, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    is_derived = Column(Boolean, default=False, nullable=False)
    maturity_level = Column(Integer, default=0, nullable=False)
    # Self-reference by id. Deliberately not a ForeignKey: the lightweight
    # ALTER-based migration in app/database.py cannot add one on SQLite.
    parent_id = Column(Integer, nullable=True, index=True)
    sort_hint = Column(Integer, default=100, nullable=False)
    taxonomy_version = Column(String(20), nullable=True)

    # Populated by app.relevance.refresh_tag_stats(). NULL until the first run.
    idf = Column(Float, nullable=True)
    assignment_count = Column(Integer, default=0, nullable=False)

    # Relationships
    movie_tags = relationship(
        "MovieTag", back_populates="tag", cascade="all, delete-orphan"
    )

    def __repr__(self):
        return f"<Tag(id={self.id}, name='{self.name}', category='{self.category}')>"


class MovieTag(Base):
    """Junction table linking movies/shows to their semantic tags."""

    __tablename__ = "movie_tags"

    tmdb_id = Column(Integer, primary_key=True)
    media_type = Column(String(10), primary_key=True)  # 'movie' or 'tv'
    tag_id = Column(
        Integer, ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True
    )
    confidence = Column(Float, nullable=False)  # 0.0 to 1.0, raw model output
    tagged_at = Column(DateTime, default=utc_now, nullable=False)

    # --- relevance (app/relevance.py) ---
    # weight      = confidence * idf(tag, media_type)
    # weight_norm = weight / ||doc||, so cosine similarity is a plain
    #               SUM(a.weight_norm * b.weight_norm) with no sqrt in the query.
    # Both are NULL until refresh_tag_stats() runs; every consumer COALESCEs to
    # confidence so an un-refreshed database still behaves.
    weight = Column(Float, nullable=True)
    weight_norm = Column(Float, nullable=True)

    # --- provenance ---
    # tagger_version drives re-tag selection: a title whose rows predate the
    # current version is picked up, which is what makes a re-tag resumable and
    # what lets an improved prompt actually reach already-tagged titles. The old
    # skip test was "has any tag at all", so prompt improvements never landed.
    tagger_version = Column(String(20), nullable=True)
    source = Column(String(16), nullable=True)  # gemini | anthropic | openai | derived
    model = Column(String(40), nullable=True)

    # Relationships
    tag = relationship("Tag", back_populates="movie_tags")

    # Indexes for fast queries
    __table_args__ = (
        Index("idx_tmdb_media", "tmdb_id", "media_type"),
        Index("idx_tag_confidence", "tag_id", "confidence"),
        Index("idx_media_confidence", "media_type", "confidence"),
        Index("idx_movie_tags_media_tag_tmdb", "media_type", "tag_id", "tmdb_id"),
        Index("idx_tagged_at", "tagged_at"),
    )

    def __repr__(self):
        return f"<MovieTag(tmdb_id={self.tmdb_id}, tag_id={self.tag_id}, confidence={self.confidence})>"


class TagAlias(Base):
    """Alternate spellings that resolve to a canonical tag.

    Seeded from ``TagDef.aliases`` and extended at runtime by an admin promoting
    an off-vocabulary observation. Operator-managed rows win over the static
    taxonomy, which is what makes the governance loop work without a redeploy.
    """

    __tablename__ = "tag_aliases"

    # app.taxonomy.normalize() output -- casefolded, punctuation stripped.
    normalized = Column(String(160), primary_key=True)
    tag_id = Column(Integer, nullable=False, index=True)
    raw = Column(String(200), nullable=True)
    source = Column(String(16), default="taxonomy", nullable=False)  # taxonomy|admin
    created_at = Column(DateTime, default=utc_now, nullable=False)

    def __repr__(self):
        return f"<TagAlias('{self.normalized}' -> {self.tag_id})>"


class TagObservation(Base):
    """Off-vocabulary strings the model produced, recorded instead of dropped.

    The pre-2.0 workers discarded anything not in the vocabulary with no log and
    no metric. This table is what the admin QA dashboard ranks by frequency so
    an operator can promote a string to an alias or add it to the taxonomy.
    """

    __tablename__ = "tag_observations"

    normalized = Column(String(200), primary_key=True)
    media_type = Column(String(10), primary_key=True)
    raw_name = Column(String(200), nullable=True)
    occurrences = Column(Integer, default=0, nullable=False)
    first_seen = Column(DateTime, default=utc_now, nullable=False)
    last_seen = Column(DateTime, default=utc_now, nullable=False)
    sample_tmdb_ids = Column(JSON, nullable=True)  # capped at 10 by the writer
    resolution = Column(String(20), default="unmapped", nullable=False)
    mapped_tag_id = Column(Integer, nullable=True)

    __table_args__ = (Index("idx_tag_obs_occurrences", "occurrences"),)


class TagStats(Base):
    """Per-tag corpus statistics, scoped by media type.

    IDF is media-scoped because the corpora are not comparable: production holds
    ~166.8k movies against ~37.2k TV titles, a 4.5:1 ratio. A single global IDF
    would systematically mis-weight every TV tag.
    """

    __tablename__ = "tag_stats"

    tag_id = Column(Integer, primary_key=True)
    media_type = Column(String(10), primary_key=True)
    doc_count = Column(Integer, default=0, nullable=False)
    corpus_size = Column(Integer, default=0, nullable=False)
    idf = Column(Float, nullable=True)
    avg_confidence = Column(Float, nullable=True)
    computed_at = Column(DateTime, default=utc_now, nullable=False)

    def __repr__(self):
        return f"<TagStats(tag_id={self.tag_id}, {self.media_type}, idf={self.idf})>"


class MediaTagVector(Base):
    """L2 norm of one title's tag weight vector.

    Stored so ``weight_norm`` can be pre-divided and cosine similarity reduces to
    a multiply-and-sum in SQL -- no sqrt at query time, which is what keeps the
    same statement running on both PostgreSQL and SQLite.
    """

    __tablename__ = "media_tag_vectors"

    tmdb_id = Column(Integer, primary_key=True)
    media_type = Column(String(10), primary_key=True)
    norm = Column(Float, nullable=False, default=1.0)
    tag_count = Column(Integer, default=0, nullable=False)
    top_tag_id = Column(Integer, nullable=True)
    updated_at = Column(DateTime, default=utc_now, nullable=False)


class UserTasteVector(Base):
    """A profile's sparse taste vector, one row per tag.

    Materialized so recommendation queries join it rather than threading dozens
    of bind parameters through Python.
    """

    __tablename__ = "user_taste_vectors"

    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    media_type = Column(String(10), primary_key=True)
    tag_id = Column(Integer, primary_key=True)
    weight = Column(Float, nullable=False)
    updated_at = Column(DateTime, default=utc_now, nullable=False)

    __table_args__ = (Index("idx_taste_vec_user", "user_id", "media_type"),)


class TitleRating(Base):
    """One rating source for one title.

    A row per source rather than a wide table, so adding IMDb/RT/Letterboxd/
    Metacritic/Trakt/MyAnimeList -- or a source that does not exist yet -- is
    data rather than a migration.
    """

    __tablename__ = "title_ratings"

    tmdb_id = Column(Integer, primary_key=True)
    media_type = Column(String(10), primary_key=True)
    source = Column(String(24), primary_key=True)  # imdb | tomatoes | metacritic | ...
    value = Column(Float, nullable=True)  # as reported by the source
    normalized = Column(Float, nullable=True)  # mapped to 0-10
    votes = Column(Integer, nullable=True)
    fetched_at = Column(DateTime, default=utc_now, nullable=False)

    __table_args__ = (Index("idx_title_ratings_item", "tmdb_id", "media_type"),)


class MdbList(Base):
    """An imported MDBList curated list.

    Membership is the only human-labelled signal in the system: a list called
    "Best Neo-Noir Films" is a set of titles a person asserted are Neo-Noir, so
    it can audit the tagger rather than merely feed it.
    """

    __tablename__ = "mdb_lists"

    slug = Column(String(160), primary_key=True)
    name = Column(String(300), nullable=True)
    description = Column(Text, nullable=True)
    media_type = Column(String(10), nullable=True)
    # Canonical tag names this list implies, e.g. ["Neo-Noir"].
    tag_hints = Column(JSON, nullable=True)
    item_count = Column(Integer, default=0, nullable=False)
    synced_at = Column(DateTime, nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)


class MdbListItem(Base):
    """Membership of an imported MDBList list."""

    __tablename__ = "mdb_list_items"

    list_slug = Column(String(160), primary_key=True)
    tmdb_id = Column(Integer, primary_key=True)
    media_type = Column(String(10), primary_key=True)
    imdb_id = Column(String(20), nullable=True)
    rank = Column(Integer, nullable=True)
    imported_at = Column(DateTime, default=utc_now, nullable=False)

    __table_args__ = (Index("idx_mdb_items_item", "tmdb_id", "media_type"),)


class MdbQuota(Base):
    """Daily MDBList request budget, persisted.

    The ceiling is discovered from response headers rather than configured, so
    the same code runs on a free 1,000/day key and a paid 500,000/day one. It is
    persisted because a restart must not rediscover the limit by walking into
    another 429.
    """

    __tablename__ = "mdb_quota"

    day = Column(String(10), primary_key=True)  # YYYY-MM-DD, UTC
    used = Column(Integer, default=0, nullable=False)
    limit_seen = Column(Integer, nullable=True)
    updated_at = Column(DateTime, default=utc_now, nullable=False)


class UniversalCategory(Base):
    """Netflix-style universal categories available to all users."""

    __tablename__ = "universal_categories"

    id = Column(String(50), primary_key=True)
    name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    tier = Column(Integer, nullable=False)  # 1-5 for organization
    sort_order = Column(Integer, nullable=False)
    media_type = Column(String(10), nullable=False)  # 'movie' or 'tv'

    # Tag matching formula
    tag_formula = Column(JSON, nullable=False)
    # Example: {"required": ["Dark", "Crime"], "optional": ["Thriller"], "min_required": 2}

    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=utc_now, nullable=False)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now, nullable=False)

    # Two tiers share this table. 'curated' rows are the 40 hand-written
    # editorial categories that seed_categories owns and re-syncs on every
    # startup; 'generated' rows come from tag co-occurrence and are owned by
    # category_synth. Without this column the seeder and the synthesizer fight
    # over the same rows on every boot.
    source = Column(String(16), default="curated", nullable=False)
    name_source = Column(String(16), nullable=True)  # template | gemini
    name_spec_hash = Column(String(40), nullable=True)
    # Which rows reach the home screen, which is hard-capped: ~180 rows is an
    # unusable client UI.
    manifest_priority = Column(Integer, default=0, nullable=False)

    # Relationships
    catalog_items = relationship(
        "UniversalCatalogContent",
        back_populates="category",
        cascade="all, delete-orphan",
    )

    def __repr__(self):
        return f"<UniversalCategory(id='{self.id}', name='{self.name}')>"


class UniversalCatalogContent(Base):
    """Pre-computed content for universal catalogs."""

    __tablename__ = "universal_catalog_content"

    category_id = Column(
        String(50),
        ForeignKey("universal_categories.id", ondelete="CASCADE"),
        primary_key=True,
    )
    tmdb_id = Column(Integer, primary_key=True)
    rank = Column(Integer, nullable=False)
    match_score = Column(Float, nullable=False)
    media_type = Column(String(10), nullable=False)  # 'movie' or 'tv'

    last_updated = Column(DateTime, default=utc_now, nullable=False)

    # Relationships
    category = relationship("UniversalCategory", back_populates="catalog_items")

    # Indexes
    __table_args__ = (
        Index("idx_category_rank", "category_id", "rank"),
        Index("idx_ucc_media_type", "media_type"),
    )

    def __repr__(self):
        return f"<UniversalCatalogContent(category_id='{self.category_id}', tmdb_id={self.tmdb_id}, rank={self.rank})>"


class SynthEditorialDecision(Base):
    """Cached AI decision for one materialized synthesized-shelf candidate.

    Rejected candidates need persistence too: without this table every rebuild
    would pay to reject the same weak concept again. ``cache_key`` includes the
    prompt/provider/model/spec/sample hashes, so a changed shelf is reviewed
    while an identical one costs nothing.
    """

    __tablename__ = "synth_editorial_decisions"

    cache_key = Column(String(64), primary_key=True)
    spec_hash = Column(String(40), nullable=False, index=True)
    sample_hash = Column(String(40), nullable=False)
    prompt_version = Column(String(20), nullable=False)
    provider = Column(String(20), nullable=False)
    model = Column(String(80), nullable=False)
    decision = Column(String(12), nullable=False)  # keep | reject
    display_name = Column(String(60), nullable=True)
    editorial_score = Column(Integer, nullable=False, default=0)
    reason_codes = Column(JSON, nullable=False, default=list)
    input_tokens = Column(Integer, nullable=False, default=0)
    output_tokens = Column(Integer, nullable=False, default=0)
    cost_usd = Column(Float, nullable=False, default=0.0)
    created_at = Column(DateTime, default=utc_now, nullable=False)

    __table_args__ = (
        Index("idx_synth_editorial_spec_sample", "spec_hash", "sample_hash"),
    )


class Account(Base):
    """A login identity that owns one or more viewing profiles (Netflix-style).

    Identity/credentials (email, password, 2FA, passkeys) live here. The
    individual viewing personas — each with its own watch history, library,
    personalized catalogs and native API ``user_key`` — are the ``User`` rows
    linked via ``User.account_id``.
    """

    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True, autoincrement=True)

    auth_source = Column(String(20), nullable=False, default="local")

    # Credentials for local auth with email/password/2FA
    email = Column(String(254), unique=True, nullable=True, index=True)
    password_hash = Column(String(128), nullable=True)
    totp_secret = Column(Text, nullable=True)  # Fernet-encrypted
    totp_enabled = Column(Boolean, default=False, nullable=False)
    # Time step of the last accepted TOTP code; refuses replays of it.
    totp_last_step = Column(BigInteger, nullable=True)

    # Passkeys are mandatory; this is the admin-granted escape hatch for someone
    # stuck on a browser without WebAuthn. NULL (the default) means no exemption.
    passkey_exempt_until = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=utc_now, nullable=False)
    last_login = Column(DateTime, default=utc_now, nullable=False)

    is_active = Column(Boolean, default=True, nullable=False)
    is_admin = Column(Boolean, default=False, nullable=False)

    # Relationships
    profiles = relationship(
        "User",
        back_populates="account",
        cascade="all, delete-orphan",
        order_by="User.created_at",
    )
    passkey_credentials = relationship(
        "PasskeyCredential", back_populates="account", cascade="all, delete-orphan"
    )

    def __repr__(self):
        return f"<Account(id={self.id}, email='{self.email}')>"


class User(Base):
    """A viewing profile under an :class:`Account`.

    Historically this table was the account itself, so it still carries the
    legacy email/password/2FA columns for non-destructive migration. New code
    treats it purely as a profile: it owns the native API ``user_key`` and all
    watch history, progress, library and personalized catalogs.
    """

    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_key = Column(String(100), unique=True, nullable=False, index=True)
    # user_key is a URL-safe token that prefixes the native API (/{user_key}/api/...)

    # Owning account (nullable during migration; enforced in app logic).
    account_id = Column(
        Integer,
        ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    auth_source = Column(String(20), nullable=False, default="local")
    # Currently 'local'; legacy rows may contain older values.

    display_name = Column(String(200), nullable=True)
    avatar = Column(String(255), nullable=True)  # profile avatar (URL or key)

    # Legacy account fields — kept for non-destructive migration only.
    # Identity now lives on Account; new code does not read these.
    email = Column(String(254), unique=True, nullable=True, index=True)
    password_hash = Column(String(128), nullable=True)
    totp_secret = Column(Text, nullable=True)  # Fernet-encrypted
    totp_enabled = Column(Boolean, default=False, nullable=False)
    bandwidth_tier = Column(
        String(10), default="high", nullable=False
    )  # 'low' or 'high'
    # What Home and search show: "everything" (discovery catalog) or
    # "my_servers" (only titles on the connected Plex/Jellyfin servers).
    content_scope = Column(String(16), default="everything", nullable=False)
    # Whether watch history from the connected Plex/Jellyfin account shapes
    # this profile's recommendations: "on", "off", or NULL = automatic (on for
    # the first profile of an admin account, i.e. the server owner).
    server_history = Column(String(8), nullable=True)

    created_at = Column(DateTime, default=utc_now, nullable=False)
    last_login = Column(DateTime, default=utc_now, nullable=False)
    last_sync = Column(DateTime, nullable=True)

    is_active = Column(Boolean, default=True, nullable=False)
    is_admin = Column(Boolean, default=False, nullable=False)

    # Relationships
    account = relationship("Account", back_populates="profiles")
    personal_catalogs = relationship(
        "UserCatalog", back_populates="user", cascade="all, delete-orphan"
    )
    watch_events = relationship(
        "WatchEvent", back_populates="user", cascade="all, delete-orphan"
    )
    watch_progress = relationship(
        "WatchProgress", back_populates="user", cascade="all, delete-orphan"
    )
    library_items = relationship(
        "LibraryItem", back_populates="user", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("idx_active_sync", "is_active", "last_sync"),
        Index("idx_users_account", "account_id"),
    )

    def __repr__(self):
        return f"<User(id={self.id}, account_id={self.account_id})>"


class PasskeyCredential(Base):
    """WebAuthn/passkey credential registered to a local user."""

    __tablename__ = "passkey_credentials"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # Passkeys are account-scoped (identity). account_id is the source of truth;
    # user_id is a legacy column retained for non-destructive migration.
    account_id = Column(
        Integer,
        ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    credential_id = Column(String(512), unique=True, nullable=False, index=True)
    public_key = Column(Text, nullable=False)
    sign_count = Column(Integer, default=0, nullable=False)
    transports = Column(JSON, nullable=True)
    nickname = Column(String(200), nullable=True)
    created_at = Column(DateTime, default=utc_now, nullable=False)
    last_used_at = Column(DateTime, nullable=True)

    account = relationship("Account", back_populates="passkey_credentials")


class PasskeyChallenge(Base):
    """Short-lived WebAuthn challenge for passkey registration or login."""

    __tablename__ = "passkey_challenges"

    token = Column(String(64), primary_key=True)
    challenge = Column(String(256), nullable=False, index=True)
    intent = Column(String(32), nullable=False)
    account_id = Column(
        Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=True
    )
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True)
    email = Column(String(254), nullable=True)
    invite_code = Column(String(64), nullable=True)
    display_name = Column(String(200), nullable=True)
    created_at = Column(DateTime, default=utc_now, nullable=False)
    expires_at = Column(DateTime, nullable=False)

    __table_args__ = (Index("idx_passkey_challenge_expiry", "expires_at"),)


class UserCatalog(Base):
    """Personalized catalogs for individual users."""

    __tablename__ = "user_catalogs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    slot_id = Column(String(50), nullable=False)
    # e.g., 'personalized-1', 'personalized-2', etc.

    name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    media_type = Column(String(10), nullable=False)  # 'movie' or 'tv'

    # How this catalog was generated
    generation_method = Column(String(50), nullable=False)
    # Methods: 'because_you_watched', 'top_picks', 'hidden_gems', etc.

    generation_params = Column(JSON, nullable=True)
    # Extra data used to generate this catalog

    last_generated = Column(DateTime, default=utc_now, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)

    # Relationships
    user = relationship("User", back_populates="personal_catalogs")
    catalog_items = relationship(
        "UserCatalogContent", back_populates="catalog", cascade="all, delete-orphan"
    )

    # Constraints
    __table_args__ = (
        UniqueConstraint("user_id", "slot_id", name="uq_user_slot"),
        Index("idx_user_active", "user_id", "is_active"),
    )

    def __repr__(self):
        return (
            f"<UserCatalog(id={self.id}, user_id={self.user_id}, name='{self.name}')>"
        )


class UserCatalogContent(Base):
    """Content for personalized user catalogs."""

    __tablename__ = "user_catalog_content"

    catalog_id = Column(
        Integer, ForeignKey("user_catalogs.id", ondelete="CASCADE"), primary_key=True
    )
    tmdb_id = Column(Integer, primary_key=True)

    rank = Column(Integer, nullable=False)
    match_score = Column(Float, nullable=False)
    media_type = Column(String(10), nullable=False)  # 'movie' or 'tv'

    added_at = Column(DateTime, default=utc_now, nullable=False)

    # Relationships
    catalog = relationship("UserCatalog", back_populates="catalog_items")

    # Indexes
    __table_args__ = (Index("idx_catalog_rank", "catalog_id", "rank"),)

    def __repr__(self):
        return f"<UserCatalogContent(catalog_id={self.catalog_id}, tmdb_id={self.tmdb_id}, rank={self.rank})>"


class MediaMetadata(Base):
    """Cached metadata from TMDB to reduce API calls."""

    __tablename__ = "media_metadata"

    tmdb_id = Column(Integer, primary_key=True)
    media_type = Column(String(10), primary_key=True)  # 'movie' or 'tv'

    title = Column(String(500), nullable=False)
    original_title = Column(String(500), nullable=True)
    overview = Column(Text, nullable=True)

    release_date = Column(String(20), nullable=True)  # YYYY-MM-DD
    genres = Column(JSON, nullable=True)  # List of genre names

    # When a title becomes watchable, for the catalog "coming soon" badge. Kept as two
    # columns because the values have different staleness: a movie's digital date is
    # fixed once TMDB announces it, while a series' next air date turns over weekly.
    digital_release_date = Column(String(20), nullable=True)  # movies, YYYY-MM-DD
    next_episode_air_date = Column(String(20), nullable=True)  # tv, YYYY-MM-DD
    # Round-robin cursor for the daily next-air-date refresh (nulls checked first).
    next_episode_checked_at = Column(DateTime, nullable=True)

    poster_path = Column(String(200), nullable=True)
    backdrop_path = Column(String(200), nullable=True)

    imdb_id = Column(String(20), nullable=True, index=True)
    logo_path = Column(String(200), nullable=True)

    original_language = Column(String(10), nullable=True)
    adult = Column(Boolean, default=False, nullable=True)

    vote_average = Column(Float, nullable=True)
    vote_count = Column(Integer, nullable=True)
    popularity = Column(Float, nullable=True)

    # For TV shows
    number_of_seasons = Column(Integer, nullable=True)
    number_of_episodes = Column(Integer, nullable=True)

    # TMDB's `belongs_to_collection` (movies only), promoted out of `raw_data` so
    # search can group a franchise without loading the payload. Search returns a
    # collection's members as one contiguous, release-ordered block rather than
    # interleaving them by rating, which is what made "harry potter" come back
    # shuffled. NULL for anything TMDB does not place in a collection.
    collection_id = Column(Integer, nullable=True, index=True)
    collection_name = Column(String(200), nullable=True)

    # Full TMDB response (for future use)
    raw_data = Column(JSON, nullable=True)

    fetched_at = Column(DateTime, default=utc_now, nullable=False)

    # --- enrichment (workers/enrich.py) ---
    # Distilled tagging signal, kept separate from raw_data so the prompt builder
    # reads a small stable shape rather than a full TMDB payload.
    tag_facts = Column(JSON, nullable=True)
    enriched_at = Column(DateTime, nullable=True)
    enrich_version = Column(String(20), nullable=True)

    # A title that produced no usable model result must not remain at the head
    # of every resumed rebuild forever. This is versioned independently from
    # successful MovieTag rows: a new tagger version may try it again, while a
    # normal resume skips the same deterministic failure. Transient provider
    # and database errors never set these fields.
    tag_attempt_version = Column(String(20), nullable=True)
    tag_attempt_status = Column(String(24), nullable=True)
    tag_attempted_at = Column(DateTime, nullable=True)

    # --- MDBList (workers/mdb_sync.py) ---
    mdb_facts = Column(JSON, nullable=True)
    mdb_enriched_at = Column(DateTime, nullable=True)
    mdb_version = Column(String(20), nullable=True)
    # Votes-weighted mean of title_ratings, 0-10. NULL until enough sources
    # exist; every consumer COALESCEs to the TMDB Bayesian value.
    rating_consensus = Column(Float, nullable=True)
    rating_source_count = Column(Integer, default=0, nullable=True)

    __table_args__ = (
        Index("idx_media_type_popularity", "media_type", "popularity"),
        Index("idx_release_date", "release_date"),
        Index("idx_ratings", "media_type", "vote_average", "vote_count"),
        Index("idx_media_vote_count_tmdb", "media_type", "vote_count", "tmdb_id"),
        Index("idx_original_language", "original_language"),
        Index("idx_media_collection", "collection_id"),
    )

    def __repr__(self):
        return f"<MediaMetadata(tmdb_id={self.tmdb_id}, media_type='{self.media_type}', title='{self.title}')>"


class HomeFeedSnapshot(Base):
    """Immutable, profile-scoped Home membership, never full metadata cards."""

    __tablename__ = "home_feed_snapshots"
    id = Column(String(36), primary_key=True)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    schema_version = Column(Integer, nullable=False, default=1)
    policy_fingerprint = Column(String(64), nullable=False)
    source_fingerprint = Column(String(64), nullable=False)
    membership = Column(JSON, nullable=False)
    created_at = Column(DateTime, nullable=False, default=utc_now)
    superseded_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("idx_home_snapshots_profile", "user_id", "created_at"),
        Index("idx_home_snapshots_superseded", "superseded_at"),
    )


class HomeFeedState(Base):
    """Durable build queue and publication pointer shared by all web workers."""

    __tablename__ = "home_feed_states"
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    generation = Column(Integer, nullable=False, default=1)
    published_generation = Column(Integer, nullable=False, default=0)
    # No FK to the snapshot: publication/retention and profile cascades stay acyclic.
    snapshot_id = Column(String(36), nullable=True)
    lease_token = Column(String(36), nullable=True)
    lease_until = Column(DateTime, nullable=True)
    retry_at = Column(DateTime, nullable=True)
    next_check_at = Column(DateTime, nullable=False, default=utc_now)
    requested_at = Column(DateTime, nullable=True)

    __table_args__ = (Index("idx_home_state_due", "next_check_at", "retry_at"),)


class DetailSnapshot(Base):
    """Persistent, provider-complete source for the native details endpoint.

    The ordinary ``MediaMetadata.raw_data`` projection is intentionally tiny and
    omits videos, image profiles, and TV episode bodies.  Native details needs all
    of those fields together, so hot titles keep a separate bounded snapshot that
    survives process restarts without making catalog queries load the large JSON.
    """

    __tablename__ = "detail_snapshots"

    tmdb_id = Column(Integer, primary_key=True)
    media_type = Column(String(10), primary_key=True)
    payload = Column(JSON, nullable=False)
    episodes_complete = Column(Boolean, default=True, nullable=False)
    schema_version = Column(Integer, default=1, nullable=False)
    refreshed_at = Column(DateTime, default=utc_now, nullable=False)
    # Freshness of the database-derived tags/ratings/similarity overlay is
    # independent from the provider snapshot above. Nullable keeps snapshots
    # written before this field was introduced immediately usable.
    overlay_refreshed_at = Column(DateTime, nullable=True)
    last_accessed_at = Column(DateTime, default=utc_now, nullable=False)

    __table_args__ = (
        Index("idx_detail_snapshots_refreshed", "refreshed_at"),
        Index("idx_detail_snapshots_accessed", "last_accessed_at"),
    )


class AdminSetting(Base):
    """Key-value store for admin-configurable settings that persist in the database."""

    __tablename__ = "admin_settings"

    key = Column(String(100), primary_key=True)
    value = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now)

    def __repr__(self):
        return f"<AdminSetting(key='{self.key}')>"


class InviteCode(Base):
    """One-time invite codes for local account registration."""

    __tablename__ = "invite_codes"

    code = Column(String(64), primary_key=True)
    created_at = Column(DateTime, default=utc_now, nullable=False)
    expires_at = Column(DateTime, nullable=True)  # None = never expires
    used_at = Column(DateTime, nullable=True)
    used_by = Column(String(100), nullable=True)  # email or display name of who used it
    is_used = Column(Boolean, default=False, nullable=False)
    label = Column(String(200), nullable=True)  # optional admin note


class TaggingJob(Base):
    """Track tagging jobs for monitoring and debugging."""

    __tablename__ = "tagging_jobs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_type = Column(String(50), nullable=False)
    # Types: 'database_build', 'weekly_update', 'manual_retag'

    started_at = Column(DateTime, default=utc_now, nullable=False)
    completed_at = Column(DateTime, nullable=True)

    items_processed = Column(Integer, default=0, nullable=False)
    items_failed = Column(Integer, default=0, nullable=False)

    status = Column(String(20), default="running", nullable=False)
    # Statuses: 'running', 'completed', 'failed', 'cancelled', 'timed_out'

    error_message = Column(Text, nullable=True)

    # Stats
    total_api_calls = Column(Integer, default=0, nullable=False)
    total_api_attempts = Column(Integer, default=0, nullable=False)
    total_tokens_used = Column(Integer, default=0, nullable=False)
    input_tokens = Column(Integer, default=0, nullable=False)
    output_tokens = Column(Integer, default=0, nullable=False)
    cached_tokens = Column(Integer, default=0, nullable=False)
    thinking_tokens = Column(Integer, default=0, nullable=False)
    usage_estimated = Column(Boolean, default=False, nullable=False)
    pricing_basis = Column(String(40), nullable=True)
    estimated_cost = Column(Float, default=0.0, nullable=False)

    job_metadata = Column(
        JSON, nullable=True
    )  # Renamed from 'metadata' (reserved by SQLAlchemy)

    def __repr__(self):
        return f"<TaggingJob(id={self.id}, job_type='{self.job_type}', status='{self.status}')>"


class WatchEvent(Base):
    """Scrobble events from Curatio clients."""

    __tablename__ = "watch_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # tmdb_id is nullable: items keyed only by an IMDb id (and not yet resolved
    # to a TMDB id) are stored imdb-only. At least one of tmdb_id / imdb_id is
    # always present (enforced in the request handlers).
    tmdb_id = Column(Integer, nullable=True)
    imdb_id = Column(String(20), nullable=True, index=True)  # e.g. 'tt1234567'
    media_type = Column(String(10), nullable=False)  # 'movie' or 'tv'

    # Episode tracking for TV
    season = Column(Integer, nullable=True)
    episode = Column(Integer, nullable=True)

    # Only 'complete' events count for recommendations
    action = Column(String(20), nullable=False)  # 'complete'

    title = Column(String(500), nullable=True)  # denormalized for display

    created_at = Column(DateTime, default=utc_now, nullable=False)

    # Relationships
    user = relationship("User", back_populates="watch_events")

    __table_args__ = (
        Index("idx_watch_user_created", "user_id", "created_at"),
        Index("idx_watch_user_tmdb", "user_id", "tmdb_id", "media_type"),
        Index("idx_watch_user_imdb", "user_id", "imdb_id"),
    )


class WatchProgress(Base):
    """Playback progress for cross-device Continue Watching.

    Upserted on every progress report from the client. One row per
    (user, tmdb_id, media_type, season, episode) — the most recent
    position always wins.
    """

    __tablename__ = "watch_progress"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # tmdb_id is nullable for imdb-only rows (see WatchEvent.tmdb_id).
    tmdb_id = Column(Integer, nullable=True)
    imdb_id = Column(String(20), nullable=True, index=True)  # e.g. 'tt1234567'
    media_type = Column(String(10), nullable=False)  # 'movie' or 'tv'

    season = Column(Integer, nullable=True)
    episode = Column(Integer, nullable=True)

    position = Column(Integer, nullable=False)  # seconds into playback
    duration = Column(Integer, nullable=False)  # total runtime in seconds

    title = Column(String(500), nullable=True)

    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now, nullable=False)

    user = relationship("User", back_populates="watch_progress")

    __table_args__ = (
        # Canonical identity for TMDB-keyed rows. NULLs are distinct, so this
        # never constrains imdb-only rows (those are deduped by the partial
        # unique index below + application logic).
        UniqueConstraint(
            "user_id",
            "tmdb_id",
            "media_type",
            "season",
            "episode",
            name="uq_progress_user_item_episode",
        ),
        Index(
            "idx_progress_user_item",
            "user_id",
            "tmdb_id",
            "media_type",
            unique=False,
        ),
        Index("idx_progress_user_imdb", "user_id", "imdb_id"),
        Index("idx_progress_user_updated", "user_id", "updated_at"),
    )


class HiddenTitle(Base):
    """A title a profile marked "Not for me".

    Hidden titles leave every discovery row and the hero, and their tags count
    slightly against similar titles in the profile's taste.
    """

    __tablename__ = "hidden_titles"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    tmdb_id = Column(Integer, nullable=False)
    media_type = Column(String(10), nullable=False)  # 'movie' or 'tv'
    created_at = Column(DateTime, default=utc_now, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "user_id", "tmdb_id", "media_type", name="uq_hidden_title_user_item"
        ),
        Index("idx_hidden_titles_user", "user_id"),
    )


class LibraryItem(Base):
    """Items the user has saved to their personal library.

    One row per (user, tmdb_id, media_type). Server is the source of
    truth — clients pull from /library and push adds/removes back.
    """

    __tablename__ = "library_items"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # tmdb_id is nullable for imdb-only rows (see WatchEvent.tmdb_id).
    tmdb_id = Column(Integer, nullable=True)
    imdb_id = Column(String(20), nullable=True, index=True)  # e.g. 'tt1234567'
    media_type = Column(String(10), nullable=False)  # 'movie' or 'tv'

    # Denormalized for offline display so the client doesn't need a
    # second TMDB lookup just to render the library list.
    title = Column(String(500), nullable=True)
    poster_path = Column(String(255), nullable=True)
    year = Column(String(8), nullable=True)

    added_at = Column(DateTime, default=utc_now, nullable=False)

    user = relationship("User", back_populates="library_items")

    __table_args__ = (
        UniqueConstraint(
            "user_id", "tmdb_id", "media_type", name="uq_library_user_item"
        ),
        Index("idx_library_user_added", "user_id", "added_at"),
        Index("idx_library_user_imdb", "user_id", "imdb_id"),
    )


class ImdbResolution(Base):
    """Persistent cache of IMDb id -> TMDB id resolutions (via TMDB ``/find``).

    ``imdb_id`` -> ``tmdb_id`` / ``media_type`` is stable, so a successful
    resolution (``status='found'``) is cached forever and never re-queried.
    Negative results (``status='not_found'``) are cached with a timestamp so
    the write path doesn't hammer TMDB on every throttled progress report, but
    can be re-attempted after a cooldown or by a background backfill — letting an
    imdb-only row upgrade to its canonical tmdb_id once TMDB knows about it.
    """

    __tablename__ = "imdb_resolutions"

    imdb_id = Column(String(20), primary_key=True)
    tmdb_id = Column(Integer, nullable=True)
    media_type = Column(String(10), nullable=True)  # 'movie' or 'tv'
    # Populated only for episode-level imdb ids (TMDB tv_episode_results).
    season = Column(Integer, nullable=True)
    episode = Column(Integer, nullable=True)

    status = Column(String(20), nullable=False, default="found")  # found|not_found
    attempts = Column(Integer, nullable=False, default=0)

    created_at = Column(DateTime, default=utc_now, nullable=False)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now, nullable=False)

    def __repr__(self):
        return (
            f"<ImdbResolution(imdb_id='{self.imdb_id}', tmdb_id={self.tmdb_id}, "
            f"status='{self.status}')>"
        )


class UserSession(Base):
    """Web login sessions, scoped to an account with a selected active profile."""

    __tablename__ = "user_sessions"

    token = Column(String(64), primary_key=True)
    # Account that authenticated; account_id is the source of truth. user_id is
    # a legacy column retained for non-destructive migration.
    account_id = Column(
        Integer,
        ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True)
    # Profile currently selected for this web session (Netflix-style switch).
    active_profile_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at = Column(DateTime, default=utc_now, nullable=False)
    expires_at = Column(DateTime, nullable=False)


class AppPairingSession(Base):
    """One-time pairing sessions for signing into the app via QR code or short code."""

    __tablename__ = "app_pairing_sessions"

    token = Column(String(64), primary_key=True)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    short_code = Column(String(8), unique=True, nullable=False)
    created_at = Column(DateTime, default=utc_now, nullable=False)
    expires_at = Column(DateTime, nullable=False)  # 5-minute expiry
    claimed = Column(Boolean, default=False, nullable=False)


class DevicePairingSession(Base):
    """Device-initiated pairing sessions (e.g. tvOS).

    Flow: device creates session (no auth) → displays code on screen →
    authenticated user claims code via web/phone → device polls until claimed.
    """

    __tablename__ = "device_pairing_sessions"

    device_token = Column(String(64), primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True)
    short_code = Column(String(8), unique=True, nullable=False)
    created_at = Column(DateTime, default=utc_now, nullable=False)
    expires_at = Column(DateTime, nullable=False)  # 5-minute expiry
    claimed = Column(Boolean, default=False, nullable=False)


class MobileAuthCode(Base):
    """Short-lived one-time authorization codes for native mobile PKCE login."""

    __tablename__ = "mobile_auth_codes"

    code = Column(String(64), primary_key=True)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    code_challenge = Column(String(128), nullable=False)
    code_challenge_method = Column(String(10), nullable=False, default="S256")
    redirect_uri = Column(String(2048), nullable=False)
    created_at = Column(DateTime, default=utc_now, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    consumed_at = Column(DateTime, nullable=True)


class EpisodeFingerprint(Base):
    __tablename__ = "episode_fingerprints"
    id = Column(Integer, primary_key=True, autoincrement=True)
    imdb_id = Column(String(20), nullable=False, index=True)
    season = Column(Integer, nullable=False)
    episode = Column(Integer, nullable=False)
    fingerprint = Column(JSON, nullable=False)
    duration_sec = Column(Float, nullable=False)
    source_url_hash = Column(String(16), nullable=True)
    status = Column(String(20), nullable=False, default="pending")
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=utc_now, nullable=False)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now)
    __table_args__ = (
        UniqueConstraint(
            "imdb_id", "season", "episode", name="uq_episode_fingerprint_identity"
        ),
        # Hot lookup filter_by(imdb_id, season, episode) + the per-season rollups.
        Index("idx_ep_fp_imdb_season_ep", "imdb_id", "season", "episode"),
        # Queue-status counts and the stale-'processing' reaper.
        Index("idx_ep_fp_status_updated", "status", "updated_at"),
    )


class SegmentMarker(Base):
    __tablename__ = "segment_markers"
    id = Column(Integer, primary_key=True, autoincrement=True)
    imdb_id = Column(String(20), nullable=False, index=True)
    season = Column(Integer, nullable=False)
    episode = Column(Integer, nullable=False)
    segment_type = Column(String(20), nullable=False)
    start_sec = Column(Float, nullable=False)
    end_sec = Column(Float, nullable=False)
    confidence = Column(Float, nullable=False, default=0.0)
    source = Column(String(30), nullable=True)
    fingerprint_version = Column(String(80), nullable=True)
    detector_version = Column(String(80), nullable=True)
    evidence = Column(JSON, nullable=True)
    is_user_provided = Column(Boolean, nullable=False, default=False)


class ExternalSegmentCache(Base):
    """One community timestamp database's answer for an episode (see
    app/intro_sources.py). `status` is hit, none or error; `segments` holds the
    normalized intro/credits entries for a hit."""

    __tablename__ = "external_segment_cache"
    id = Column(Integer, primary_key=True, autoincrement=True)
    imdb_id = Column(String(20), nullable=False)
    season = Column(Integer, nullable=False)
    episode = Column(Integer, nullable=False)
    source = Column(String(20), nullable=False)
    status = Column(String(10), nullable=False)
    segments = Column(JSON, nullable=True)
    fetched_at = Column(DateTime, default=utc_now, nullable=False)
    __table_args__ = (
        UniqueConstraint(
            "imdb_id",
            "season",
            "episode",
            "source",
            name="uq_external_segment_cache_identity",
        ),
    )


class SegmentFeedback(Base):
    __tablename__ = "segment_feedback"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    imdb_id = Column(String(20), nullable=False)
    season = Column(Integer, nullable=False)
    episode = Column(Integer, nullable=False)
    segment_type = Column(String(20), nullable=False)
    feedback = Column(String(10), nullable=False)
    adjusted_start = Column(Float, nullable=True)
    adjusted_end = Column(Float, nullable=True)


class SeasonSegmentReference(Base):
    __tablename__ = "season_segment_references"
    id = Column(Integer, primary_key=True, autoincrement=True)
    imdb_id = Column(String(20), nullable=False, index=True)
    season = Column(Integer, nullable=False)
    segment_type = Column(String(20), nullable=False)
    fingerprint = Column(JSON, nullable=False)
    duration_sec = Column(Float, nullable=False)
    confidence = Column(Float, nullable=False)
    derived_from_episodes = Column(Integer, nullable=False)
    __table_args__ = (UniqueConstraint("imdb_id", "season", "segment_type"),)


class SeasonLearningState(Base):
    """Persists matcher diagnostics for each (imdb_id, season) learning attempt."""

    __tablename__ = "season_learning_state"
    id = Column(Integer, primary_key=True, autoincrement=True)
    imdb_id = Column(String(20), nullable=False)
    season = Column(Integer, nullable=False)
    done_episodes = Column(JSON, nullable=True)
    selected_episodes = Column(JSON, nullable=True)
    intro_pair_hits = Column(Integer, default=0)
    credits_pair_hits = Column(Integer, default=0)
    last_reason = Column(String(100), nullable=True)
    diagnostics = Column(JSON, nullable=True)
    attempted_at = Column(DateTime, default=utc_now, onupdate=utc_now)
    last_prelearning_reason = Column(String(100), nullable=True)
    last_prelearning_at = Column(DateTime, nullable=True)
    __table_args__ = (UniqueConstraint("imdb_id", "season"),)


class MediaAvailability(Base):
    """One title present on a connected media server (Plex or Jellyfin).

    Filled by each library provider's sync job. A row means "this server has
    this movie, or at least one episode of this show". Availability checks are
    a single indexed lookup by IMDb or TMDB id, so payload builders can flag
    whole rows of cards without touching the network.
    """

    __tablename__ = "media_availability"

    id = Column(Integer, primary_key=True, autoincrement=True)
    provider = Column(String(32), nullable=False)
    server_id = Column(String(128), nullable=False)
    item_key = Column(String(128), nullable=False)
    media_type = Column(String(10), nullable=False)  # movie | series
    imdb_id = Column(String(20), nullable=True, index=True)
    tmdb_id = Column(Integer, nullable=True, index=True)
    tvdb_id = Column(Integer, nullable=True)
    title = Column(String(500), nullable=True)
    year = Column(Integer, nullable=True)
    # When the title arrived on the media server (Plex addedAt, Jellyfin
    # DateCreated). Orders the "On Your Servers" row and Library tab.
    added_at = Column(DateTime, nullable=True)
    # Best copy on the server ("2160p", "1080p", ...) when the listing says so.
    resolution = Column(String(10), nullable=True)
    # Server-relative artwork path, served through the signed /media/ proxy.
    thumb = Column(String(500), nullable=True)
    # Watch state of the server account Curatio signed in with. play_count is
    # the number of plays for a movie and of watched episodes for a show
    # (Jellyfin series: 1 = started, 3 = fully watched). 0 = never played.
    play_count = Column(Integer, nullable=True)
    watched_at = Column(DateTime, nullable=True)
    sync_generation = Column(Integer, default=0, nullable=False)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "provider", "server_id", "item_key", name="uq_media_availability_item"
        ),
        Index("idx_media_availability_provider_gen", "provider", "sync_generation"),
    )


class SourceLookupMiss(Base):
    """Titles and episodes that resolved to zero playback sources.

    One row per media key, counted up, so "what do people try to play that no
    connected source has" is a query instead of an investigation.
    """

    __tablename__ = "source_lookup_misses"

    media_key = Column(String(80), primary_key=True)
    media_type = Column(String(10), nullable=False)
    imdb_id = Column(String(20), nullable=True, index=True)
    tmdb_id = Column(Integer, nullable=True)
    season = Column(Integer, nullable=True)
    episode = Column(Integer, nullable=True)
    title = Column(String(500), nullable=True)
    miss_count = Column(Integer, default=1, nullable=False)
    first_seen_at = Column(DateTime, default=utc_now, nullable=False)
    last_seen_at = Column(DateTime, default=utc_now, onupdate=utc_now, nullable=False)

    __table_args__ = (Index("idx_source_lookup_misses_last_seen", "last_seen_at"),)


class ProviderFailureEvent(Base):
    """One row per client-reported playback failure, keyed by provider id.

    Queried as a rolling window to decide which providers are currently degraded,
    so a struggling provider's sources sort behind healthy ones.
    """

    __tablename__ = "provider_failure_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    provider_key = Column(String(64), nullable=False, index=True)
    reason = Column(String(64), nullable=True)
    occurred_at = Column(DateTime, default=utc_now, nullable=False)

    __table_args__ = (
        Index("idx_provider_failure_events_key_time", "provider_key", "occurred_at"),
    )
