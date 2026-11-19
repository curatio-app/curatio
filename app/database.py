"""
Database connection and session management.

Provides database engine, session factory, and helper functions.
"""

from contextlib import contextmanager
import json
from contextvars import ContextVar
from datetime import datetime, timedelta
from typing import Any, Generator
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.pool import Pool
from loguru import logger
import orjson

from app.time_utils import utc_now  # noqa: E402
from app.config import settings
from app.models import Base


def _pg_session_options(
    lock_timeout_ms: int = 0,
    statement_timeout_ms: int = 0,
    idle_in_transaction_ms: int = 0,
) -> str:
    """libpq ``options`` setting per-connection timeouts (0 leaves one off)."""
    settings_ = {
        "lock_timeout": lock_timeout_ms,
        "statement_timeout": statement_timeout_ms,
        "idle_in_transaction_session_timeout": idle_in_transaction_ms,
    }
    return " ".join(f"-c {name}={value}" for name, value in settings_.items())


def _json_serializer(value: Any) -> str:
    """orjson for JSON columns, falling back to stdlib for what it rejects.

    Detail snapshots carry whole series (hundreds of episodes); stdlib ``json``
    encoding and decoding of those payloads was a measurable share of every
    snapshot read and write. orjson refuses a few inputs stdlib accepts (e.g.
    integers beyond 64 bits), so those keep the previous behaviour.
    """
    try:
        return orjson.dumps(value, option=orjson.OPT_NON_STR_KEYS).decode()
    except TypeError:
        return json.dumps(value)


# Create database engine. SQLite's default SingletonPool does not accept the
# QueuePool-only sizing/timeout arguments used by production Postgres. Keeping
# those options conditional makes the application importable in local tools and
# in-memory tests without changing the production pool contract.
_engine_options: dict[str, Any] = {
    "json_serializer": _json_serializer,
    "json_deserializer": orjson.loads,
    "pool_pre_ping": True,
    "pool_recycle": 1800,
    "echo": settings.debug,
}
if settings.database_url.startswith("sqlite"):
    _engine_options["connect_args"] = {"check_same_thread": False}
else:
    _engine_options.update(
        {
            "pool_size": settings.db_pool_size,
            "max_overflow": settings.db_max_overflow,
            # Fail fast under pool contention instead of the 30s default.
            "pool_timeout": settings.db_pool_timeout,
            # A request must never wait out a background job's row lock or keep
            # a connection idle in a transaction across provider I/O.
            "connect_args": {
                "options": _pg_session_options(
                    lock_timeout_ms=settings.db_request_lock_timeout_ms,
                    statement_timeout_ms=settings.db_request_statement_timeout_ms,
                    idle_in_transaction_ms=settings.db_request_idle_in_transaction_ms,
                )
            },
        }
    )

engine = create_engine(settings.database_url, **_engine_options)

# Background jobs (catalog syncs, the daily update, home-feed builders, warmers,
# startup migrations) get their own pool without the request-path timeouts, so
# a long rebuild can neither pin request connections nor be killed by a limit
# meant for interactive traffic. SQLite (tests, local tools) has a single file
# and keeps one engine.
if settings.database_url.startswith("sqlite"):
    job_engine = engine
else:
    job_engine = create_engine(
        settings.database_url,
        json_serializer=_json_serializer,
        json_deserializer=orjson.loads,
        pool_pre_ping=True,
        pool_recycle=1800,
        echo=settings.debug,
        pool_size=settings.db_job_pool_size,
        max_overflow=settings.db_job_max_overflow,
        pool_timeout=60,
        connect_args={
            "options": _pg_session_options(
                idle_in_transaction_ms=settings.db_job_idle_in_transaction_ms,
            )
        },
    )

_job_scope: ContextVar[bool] = ContextVar("curatio_db_job_scope", default=False)


@contextmanager
def job_db_scope() -> Generator[None, None, None]:
    """Route sessions opened in this context to the background-job engine.

    ``asyncio`` tasks and ``asyncio.to_thread`` copy the current context, so a
    job's private loop and its offloaded helpers inherit the routing.
    """
    token = _job_scope.set(True)
    try:
        yield
    finally:
        _job_scope.reset(token)


def active_engine():
    """The engine sessions opened in the current context bind to."""
    return job_engine if _job_scope.get() else engine


def pool_occupancy() -> dict[str, int | str | None]:
    """Return a non-throwing snapshot suitable for request/job logs."""
    pool = engine.pool

    def metric(name: str) -> int | None:
        value = getattr(pool, name, None)
        try:
            value = value() if callable(value) else value
            return int(value) if value is not None else None
        except Exception:
            return None

    return {
        "class": type(pool).__name__,
        "size": metric("size"),
        "checked_in": metric("checkedin"),
        "checked_out": metric("checkedout"),
        "overflow": metric("overflow"),
        "configured_overflow": (
            None
            if settings.database_url.startswith("sqlite")
            else settings.db_max_overflow
        ),
    }


# Listen for connection events (useful for debugging)
@event.listens_for(Pool, "connect")
def set_sqlite_pragma(dbapi_conn, connection_record):
    """Set SQLite pragmas if using SQLite (for testing)."""
    if "sqlite" in settings.database_url:
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


class _RoutingSessionmaker(sessionmaker):
    """Bind new sessions to the job engine inside :func:`job_db_scope`."""

    def __call__(self, **local_kw):
        if "bind" not in local_kw and _job_scope.get():
            local_kw["bind"] = job_engine
        return super().__call__(**local_kw)


# Session factory
SessionLocal = _RoutingSessionmaker(
    autocommit=False, autoflush=False, expire_on_commit=False, bind=engine
)


def create_tables():
    """Create all database tables."""
    logger.info("Creating database tables...")
    Base.metadata.create_all(bind=active_engine())
    logger.info("Database tables created successfully")


def drop_tables():
    """Drop all database tables (use with caution!)."""
    logger.warning("Dropping all database tables...")
    Base.metadata.drop_all(bind=engine)
    logger.warning("All database tables dropped")


@contextmanager
def get_db() -> Generator[Session, None, None]:
    """
    Get database session as context manager.

    Usage:
        with get_db() as db:
            user = db.query(User).first()
    """
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Database error: {e}")
        raise
    finally:
        db.close()


def get_db_session() -> Session:
    """
    Get database session (must be closed manually).

    Usage:
        db = get_db_session()
        try:
            user = db.query(User).first()
            db.commit()
        finally:
            db.close()
    """
    return SessionLocal()


# Dependency for FastAPI
def get_db_dependency():
    """
    FastAPI dependency for database session.

    Usage:
        @app.get("/users")
        def get_users(db: Session = Depends(get_db_dependency)):
            return db.query(User).all()
    """
    db = SessionLocal()
    try:
        yield db
    except Exception:
        # Roll back a failed request's pending writes before the session is
        # returned to the pool (symmetric with get_db()); handlers still commit
        # their own successful writes explicitly.
        db.rollback()
        raise
    finally:
        db.close()


def check_database_connection() -> bool:
    """Check if database is accessible."""
    try:
        with get_db() as db:
            # Execute simple query
            db.execute(text("SELECT 1"))
        logger.info("Database connection successful")
        return True
    except Exception as e:
        logger.error(f"Database connection failed: {e}")
        return False


def init_database():
    """Initialize database (create tables and seed categories)."""
    # Migrations and seeding run on the job engine: the request-path lock and
    # statement timeouts would abort a legitimate DDL wait at startup.
    with job_db_scope():
        return _init_database()


def _init_database():
    lock_conn = None
    try:
        if engine.dialect.name == "postgresql":
            lock_conn = job_engine.connect()
            lock_conn.execute(text("SELECT pg_advisory_lock(28740101)"))
            lock_conn.commit()
        # Check connection
        if not check_database_connection():
            raise Exception("Cannot connect to database")

        # Create tables
        create_tables()

        # Add any missing columns to existing tables (lightweight migration)
        _add_missing_columns()
        _reset_stale_fingerprint_jobs()

        # Reconcile the tag vocabulary, then retain inactive compatibility
        # definitions for legacy category ids. Home rows use synthesized
        # rows only. Taxonomy first: formulas reference tags by name.
        from app.categories import seed_categories
        from app.taxonomy_sync import sync_taxonomy

        db = SessionLocal()
        try:
            sync_taxonomy(db)
            seed_categories(db)
        finally:
            db.close()

        logger.info("Database initialized successfully")
        return True
    except Exception as e:
        logger.error(f"Database initialization failed: {e}")
        raise
    finally:
        if lock_conn is not None:
            try:
                lock_conn.execute(text("SELECT pg_advisory_unlock(28740101)"))
                lock_conn.commit()
            finally:
                lock_conn.close()


def _pg_column_is_nullable(db: Session, table: str, column: str) -> bool:
    """True when a Postgres column already allows NULL (or does not exist)."""
    value = db.execute(
        text(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_schema = current_schema() "
            "AND table_name = :table AND column_name = :column"
        ),
        {"table": table, "column": column},
    ).scalar()
    return value is None or value == "YES"


def _pg_primary_key_columns(db: Session, table: str) -> list[str]:
    """A Postgres table's primary-key columns in key order."""
    rows = db.execute(
        text(
            "SELECT a.attname FROM pg_index i "
            "JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS k(attnum, ord) ON true "
            "JOIN pg_attribute a "
            "ON a.attrelid = i.indrelid AND a.attnum = k.attnum "
            "WHERE i.indrelid = to_regclass(:table) AND i.indisprimary "
            "ORDER BY k.ord"
        ),
        {"table": table},
    ).all()
    return [str(row[0]) for row in rows]


def _add_missing_columns():
    """Add columns that exist in models but not yet in the database."""
    migrations = [
        ("media_metadata", "original_language", "VARCHAR(10)"),
        ("media_metadata", "adult", "BOOLEAN DEFAULT FALSE"),
        ("media_metadata", "overview", "TEXT"),
        ("media_metadata", "backdrop_path", "VARCHAR(200)"),
        ("media_metadata", "number_of_seasons", "INTEGER"),
        ("media_metadata", "number_of_episodes", "INTEGER"),
        ("media_metadata", "raw_data", "JSON"),
        ("media_metadata", "imdb_id", "VARCHAR(20)"),
        ("media_metadata", "logo_path", "VARCHAR(200)"),
        ("media_metadata", "digital_release_date", "VARCHAR(20)"),
        ("media_metadata", "next_episode_air_date", "VARCHAR(20)"),
        ("media_metadata", "next_episode_checked_at", "TIMESTAMP"),
        (
            "detail_snapshots",
            "overlay_refreshed_at",
            "TIMESTAMP",
        ),
        # Franchise grouping in search. Backfilled from the stored
        # `belongs_to_collection` by `_backfill_collections`, so no re-fetch.
        ("media_metadata", "collection_id", "INTEGER"),
        ("media_metadata", "collection_name", "VARCHAR(200)"),
        ("accounts", "totp_last_step", "BIGINT"),
        ("media_availability", "added_at", "TIMESTAMP"),
        ("media_availability", "resolution", "VARCHAR(10)"),
        ("media_availability", "thumb", "VARCHAR(500)"),
        ("users", "content_scope", "VARCHAR(16) DEFAULT 'everything' NOT NULL"),
        ("users", "server_history", "VARCHAR(8)"),
        ("media_availability", "play_count", "INTEGER"),
        ("media_availability", "watched_at", "TIMESTAMP"),
    ]
    with get_db() as db:
        for table, column, col_type in migrations:
            try:
                db.execute(text(f"SELECT {column} FROM {table} LIMIT 1"))
            except Exception:
                db.rollback()
                logger.info(f"Adding missing column {table}.{column}")
                if engine.dialect.name == "postgresql":
                    db.execute(
                        text(
                            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {col_type}"
                        )
                    )
                else:
                    db.execute(
                        text(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
                    )
                db.commit()
        # Ensure index on imdb_id
        try:
            db.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS idx_imdb_id ON media_metadata (imdb_id)"
                )
            )
            db.commit()
        except Exception:
            db.rollback()

        try:
            db.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS idx_movie_tags_media_tag_tmdb "
                    "ON movie_tags (media_type, tag_id, tmdb_id)"
                )
            )
            db.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS idx_media_vote_count_tmdb "
                    "ON media_metadata (media_type, vote_count, tmdb_id)"
                )
            )
            db.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS idx_media_collection "
                    "ON media_metadata (collection_id)"
                )
            )
            db.commit()
        except Exception as e:
            db.rollback()
            logger.warning(f"Could not create catalog speed indexes: {e}")

        # --- User model migrations for scrobble support ---
        user_migrations = [
            ("users", "auth_source", "VARCHAR(20) DEFAULT 'local' NOT NULL"),
            ("users", "display_name", "VARCHAR(200)"),
        ]
        for table, column, col_type in user_migrations:
            try:
                db.execute(text(f"SELECT {column} FROM {table} LIMIT 1"))
            except Exception:
                db.rollback()
                logger.info(f"Adding missing column {table}.{column}")
                db.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}"))
                db.commit()

        # --- v2: Account system columns ---
        v2_user_migrations = [
            ("users", "email", "VARCHAR(254)"),
            ("users", "password_hash", "VARCHAR(128)"),
            ("users", "totp_secret", "TEXT"),
            ("users", "totp_enabled", "BOOLEAN DEFAULT FALSE NOT NULL"),
            ("users", "bandwidth_tier", "VARCHAR(10) DEFAULT 'high' NOT NULL"),
            ("users", "is_admin", "BOOLEAN DEFAULT FALSE NOT NULL"),
            ("admin_sessions", "user_id", "INTEGER"),
        ]
        for table, column, col_type in v2_user_migrations:
            try:
                db.execute(text(f"SELECT {column} FROM {table} LIMIT 1"))
            except Exception:
                db.rollback()
                logger.info(f"Adding missing column {table}.{column}")
                db.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}"))
                db.commit()

        # Unique index on email (only non-NULL for imported/legacy users)
        try:
            db.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email_unique "
                    "ON users (email) WHERE email IS NOT NULL"
                )
            )
            db.commit()
        except Exception:
            db.rollback()

        # --- v3: Multi-profile accounts ---
        # The accounts table itself is created by create_all(). Here we add the
        # linking columns and relax legacy NOT NULL constraints so identity can
        # move up to Account while users.* becomes a viewing profile.
        v3_migrations = [
            ("users", "account_id", "INTEGER"),
            ("users", "avatar", "VARCHAR(255)"),
            ("passkey_credentials", "account_id", "INTEGER"),
            ("passkey_challenges", "account_id", "INTEGER"),
            ("user_sessions", "account_id", "INTEGER"),
            ("user_sessions", "active_profile_id", "INTEGER"),
        ]
        for table, column, col_type in v3_migrations:
            try:
                db.execute(text(f"SELECT {column} FROM {table} LIMIT 1"))
            except Exception:
                db.rollback()
                logger.info(f"Adding missing column {table}.{column}")
                if engine.dialect.name == "postgresql":
                    db.execute(
                        text(
                            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {col_type}"
                        )
                    )
                else:
                    db.execute(
                        text(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
                    )
                db.commit()

        if engine.dialect.name == "postgresql":
            # Legacy columns were NOT NULL when they pointed at users; new rows key
            # off account_id instead, so drop the constraints.
            for table, column in (
                ("passkey_credentials", "user_id"),
                ("user_sessions", "user_id"),
            ):
                if _pg_column_is_nullable(db, table, column):
                    continue
                try:
                    db.execute(
                        text(f"ALTER TABLE {table} ALTER COLUMN {column} DROP NOT NULL")
                    )
                    db.commit()
                except Exception:
                    db.rollback()
            try:
                db.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS idx_users_account "
                        "ON users (account_id)"
                    )
                )
                db.commit()
            except Exception:
                db.rollback()

        _backfill_accounts(db)

        # --- v4: Mandatory passkeys ---
        # Admin-granted exemption for accounts that cannot enroll a passkey.
        v4_migrations = [
            ("accounts", "passkey_exempt_until", "TIMESTAMP"),
        ]
        for table, column, col_type in v4_migrations:
            try:
                db.execute(text(f"SELECT {column} FROM {table} LIMIT 1"))
            except Exception:
                db.rollback()
                logger.info(f"Adding missing column {table}.{column}")
                if engine.dialect.name == "postgresql":
                    db.execute(
                        text(
                            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {col_type}"
                        )
                    )
                else:
                    db.execute(
                        text(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
                    )
                db.commit()

        # --- Fingerprint learning diagnostics columns ---
        # create_all() creates the table for fresh installs, but existing deployments
        # need lightweight ALTER migrations when the SeasonLearningState model grows.
        season_learning_migrations = [
            ("season_learning_state", "done_episodes", "JSON"),
            ("season_learning_state", "selected_episodes", "JSON"),
            ("season_learning_state", "intro_pair_hits", "INTEGER DEFAULT 0"),
            ("season_learning_state", "credits_pair_hits", "INTEGER DEFAULT 0"),
            ("season_learning_state", "last_reason", "VARCHAR(100)"),
            (
                "season_learning_state",
                "diagnostics",
                (
                    "JSONB DEFAULT '{}'::jsonb"
                    if engine.dialect.name == "postgresql"
                    else "JSON"
                ),
            ),
            ("season_learning_state", "attempted_at", "TIMESTAMP"),
            ("season_learning_state", "last_prelearning_reason", "VARCHAR(100)"),
            ("season_learning_state", "last_prelearning_at", "TIMESTAMP"),
        ]
        for table, column, col_type in season_learning_migrations:
            try:
                db.execute(text(f"SELECT {column} FROM {table} LIMIT 1"))
            except Exception:
                db.rollback()
                logger.info(f"Adding missing column {table}.{column}")
                if engine.dialect.name == "postgresql":
                    db.execute(
                        text(
                            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {col_type}"
                        )
                    )
                else:
                    db.execute(
                        text(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
                    )
                db.commit()

        # Composite indexes for the hot episode-fingerprint lookups + status rollups
        # (mirror EpisodeFingerprint.__table_args__ for existing deployments).
        for ddl in (
            "CREATE INDEX IF NOT EXISTS idx_ep_fp_imdb_season_ep "
            "ON episode_fingerprints (imdb_id, season, episode)",
            "CREATE INDEX IF NOT EXISTS idx_ep_fp_status_updated "
            "ON episode_fingerprints (status, updated_at)",
        ):
            try:
                db.execute(text(ddl))
                db.commit()
            except Exception as e:
                db.rollback()
                logger.warning(f"Could not create episode_fingerprint index: {e}")

        episode_fingerprint_migrations = [
            ("episode_fingerprints", "source_url_hash", "TEXT"),
        ]
        for table, column, col_type in episode_fingerprint_migrations:
            try:
                db.execute(text(f"SELECT {column} FROM {table} LIMIT 1"))
            except Exception:
                db.rollback()
                logger.info(f"Adding missing column {table}.{column}")
                if engine.dialect.name == "postgresql":
                    db.execute(
                        text(
                            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {col_type}"
                        )
                    )
                else:
                    db.execute(
                        text(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
                    )
                db.commit()

        # A stream-open and a prelearning trigger can race on the same episode. Merge
        # compatible phase payloads before enforcing identity uniqueness so existing
        # deployments retain the most complete/current work instead of an arbitrary row.
        try:
            from app.models import EpisodeFingerprint

            duplicate_keys = db.execute(
                text(
                    "SELECT imdb_id, season, episode FROM episode_fingerprints "
                    "GROUP BY imdb_id, season, episode HAVING COUNT(*) > 1"
                )
            ).all()
            for imdb_id, season, episode in duplicate_keys:
                rows = (
                    db.query(EpisodeFingerprint)
                    .filter_by(imdb_id=imdb_id, season=season, episode=episode)
                    .all()
                )

                def completeness(row):
                    payload = row.fingerprint or {}
                    audio_current = (
                        payload.get("fingerprint_version") == "chromaprint-v2"
                    )
                    phase_count = sum(
                        (
                            bool(payload.get("head")) and audio_current,
                            payload.get("tail_visual_version") == "credits-visual-v3",
                        )
                    )
                    detector_current = payload.get("detector_version") == "hybrid-v8"
                    status_rank = {"done": 3, "partial": 2, "processing": 1}.get(
                        row.status, 0
                    )
                    return (
                        phase_count,
                        int(detector_current),
                        status_rank,
                        row.updated_at or row.created_at or datetime.min,
                        row.id,
                    )

                winner = max(rows, key=completeness)
                merged = dict(winner.fingerprint or {})
                for candidate in sorted(rows, key=completeness, reverse=True):
                    if candidate.id == winner.id:
                        continue
                    if candidate.source_url_hash == winner.source_url_hash:
                        for key, value in (candidate.fingerprint or {}).items():
                            if key not in merged or merged[key] in (None, [], {}):
                                merged[key] = value
                    db.delete(candidate)
                winner.fingerprint = merged
            db.commit()
            db.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_episode_fingerprint_identity "
                    "ON episode_fingerprints (imdb_id, season, episode)"
                )
            )
            db.commit()
        except Exception as e:
            db.rollback()
            logger.warning(f"Could not deduplicate episode fingerprints: {e}")

        # Keep movies and shows with the same TMDB ID from colliding in tag rows.
        if engine.dialect.name == "postgresql":
            # Rebuilding this key takes an ACCESS EXCLUSIVE lock and rewrites the
            # index of one of the largest tables, so only do it when it is wrong;
            # it used to run on every startup.
            try:
                if _pg_primary_key_columns(db, "movie_tags") != [
                    "tmdb_id",
                    "media_type",
                    "tag_id",
                ]:
                    db.execute(
                        text(
                            "ALTER TABLE movie_tags "
                            "DROP CONSTRAINT IF EXISTS movie_tags_pkey"
                        )
                    )
                    db.execute(
                        text(
                            "ALTER TABLE movie_tags ADD CONSTRAINT movie_tags_pkey "
                            "PRIMARY KEY (tmdb_id, media_type, tag_id)"
                        )
                    )
                    db.commit()
            except Exception as e:
                db.rollback()
                logger.warning(f"Could not migrate movie_tags primary key: {e}")

            try:
                db.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS idx_media_title_trgm "
                        "ON media_metadata USING gin (title gin_trgm_ops)"
                    )
                )
                db.commit()
            except Exception:
                db.rollback()
                try:
                    db.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
                    db.execute(
                        text(
                            "CREATE INDEX IF NOT EXISTS idx_media_title_trgm "
                            "ON media_metadata USING gin (title gin_trgm_ops)"
                        )
                    )
                    db.commit()
                except Exception as e:
                    db.rollback()
                    logger.warning(f"Could not create trigram search index: {e}")

            try:
                db.execute(
                    text(
                        "CREATE UNIQUE INDEX IF NOT EXISTS uq_progress_user_item_episode "
                        "ON watch_progress (user_id, tmdb_id, media_type, "
                        "COALESCE(season, 0), COALESCE(episode, 0))"
                    )
                )
                db.commit()
            except Exception as e:
                db.rollback()
                logger.warning(f"Could not create progress uniqueness index: {e}")

        segment_marker_migrations = [
            ("segment_markers", "source", "VARCHAR(30)"),
            ("segment_markers", "fingerprint_version", "VARCHAR(80)"),
            ("segment_markers", "detector_version", "VARCHAR(80)"),
            (
                "segment_markers",
                "evidence",
                "JSONB" if engine.dialect.name == "postgresql" else "JSON",
            ),
            ("segment_markers", "is_user_provided", "BOOLEAN DEFAULT FALSE"),
        ]
        for table, column, col_type in segment_marker_migrations:
            try:
                db.execute(text(f"SELECT {column} FROM {table} LIMIT 1"))
            except Exception:
                db.rollback()
                logger.info(f"Adding missing column {table}.{column}")
                if engine.dialect.name == "postgresql":
                    db.execute(
                        text(
                            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {col_type}"
                        )
                    )
                else:
                    db.execute(
                        text(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
                    )
                db.commit()

        # --- IMDb-keyed sync: add imdb_id columns to the sync tables ---
        # Lets items keyed only by an IMDb id sync cross-device. create_all()
        # handles fresh installs and the new imdb_resolutions table; these
        # ALTERs cover existing deployments.
        imdb_sync_migrations = [
            ("watch_progress", "imdb_id", "VARCHAR(20)"),
            ("watch_events", "imdb_id", "VARCHAR(20)"),
            ("library_items", "imdb_id", "VARCHAR(20)"),
        ]
        for table, column, col_type in imdb_sync_migrations:
            try:
                db.execute(text(f"SELECT {column} FROM {table} LIMIT 1"))
            except Exception:
                db.rollback()
                logger.info(f"Adding missing column {table}.{column}")
                if engine.dialect.name == "postgresql":
                    db.execute(
                        text(
                            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {col_type}"
                        )
                    )
                else:
                    db.execute(
                        text(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
                    )
                db.commit()

        if engine.dialect.name == "postgresql":
            # tmdb_id was NOT NULL when every item had a TMDB id; imdb-only rows
            # now store it NULL, so relax the constraint on existing tables.
            for table in ("watch_progress", "watch_events", "library_items"):
                if _pg_column_is_nullable(db, table, "tmdb_id"):
                    continue
                try:
                    db.execute(
                        text(f"ALTER TABLE {table} ALTER COLUMN tmdb_id DROP NOT NULL")
                    )
                    db.commit()
                except Exception:
                    db.rollback()

            # Lookup + dedup indexes for imdb-keyed rows. The partial unique
            # indexes keep imdb-only rows (tmdb_id NULL) from duplicating under
            # concurrent writes; once a row upgrades to a tmdb_id it leaves the
            # partial index and the tmdb uniqueness constraint takes over.
            imdb_index_ddl = [
                "CREATE INDEX IF NOT EXISTS idx_progress_user_imdb "
                "ON watch_progress (user_id, imdb_id)",
                "CREATE INDEX IF NOT EXISTS idx_watch_user_imdb "
                "ON watch_events (user_id, imdb_id)",
                "CREATE INDEX IF NOT EXISTS idx_library_user_imdb "
                "ON library_items (user_id, imdb_id)",
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_progress_user_imdb_only "
                "ON watch_progress (user_id, imdb_id, "
                "COALESCE(season, 0), COALESCE(episode, 0)) "
                "WHERE imdb_id IS NOT NULL AND tmdb_id IS NULL",
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_library_user_imdb_only "
                "ON library_items (user_id, imdb_id) "
                "WHERE imdb_id IS NOT NULL AND tmdb_id IS NULL",
            ]
            for ddl in imdb_index_ddl:
                try:
                    db.execute(text(ddl))
                    db.commit()
                except Exception as e:
                    db.rollback()
                    logger.warning(f"Could not create imdb sync index: {e}")

        # --- taxonomy 2.0 (app/taxonomy.py) ---
        # DDL ONLY. The weight/weight_norm population is a ~2.2M row UPDATE and
        # must never run from here: init_database() holds a pg_advisory_lock and
        # blocks startup. app.relevance.refresh_tag_stats() owns that work and is
        # driven by a worker entrypoint or the admin endpoint.
        #
        # The three weight_norm indexes are likewise NOT created here -- they are
        # built by refresh_tag_stats() *after* the population UPDATE. Creating
        # them first would make every row rewrite non-HOT and roughly double the
        # size of both large indexes.
        tagging_v2_migrations = [
            ("tags", "display", "BOOLEAN DEFAULT TRUE NOT NULL"),
            ("tags", "is_active", "BOOLEAN DEFAULT TRUE NOT NULL"),
            ("tags", "is_derived", "BOOLEAN DEFAULT FALSE NOT NULL"),
            ("tags", "maturity_level", "INTEGER DEFAULT 0 NOT NULL"),
            ("tags", "parent_id", "INTEGER"),
            ("tags", "sort_hint", "INTEGER DEFAULT 100 NOT NULL"),
            ("tags", "taxonomy_version", "VARCHAR(20)"),
            ("tags", "idf", "DOUBLE PRECISION"),
            ("tags", "assignment_count", "INTEGER DEFAULT 0 NOT NULL"),
            ("movie_tags", "weight", "DOUBLE PRECISION"),
            ("movie_tags", "weight_norm", "DOUBLE PRECISION"),
            ("movie_tags", "tagger_version", "VARCHAR(20)"),
            ("movie_tags", "source", "VARCHAR(16)"),
            ("movie_tags", "model", "VARCHAR(40)"),
            ("media_metadata", "tag_facts", "JSON"),
            ("media_metadata", "enriched_at", "TIMESTAMP"),
            ("media_metadata", "enrich_version", "VARCHAR(20)"),
            ("media_metadata", "tag_attempt_version", "VARCHAR(20)"),
            ("media_metadata", "tag_attempt_status", "VARCHAR(24)"),
            ("media_metadata", "tag_attempted_at", "TIMESTAMP"),
            ("media_metadata", "mdb_facts", "JSON"),
            ("media_metadata", "mdb_enriched_at", "TIMESTAMP"),
            # Versions the MDBList fetch the way enrich_version versions the
            # TMDB one. Without it the resume query was "mdb_enriched_at IS
            # NULL", so a title fetched by an older, wronger client was skipped
            # forever -- which is exactly what happened to the rows fetched
            # before the request asked for reviews. NULL here means "fetched by
            # a client that predates versioning", so those rows re-fetch once.
            ("media_metadata", "mdb_version", "VARCHAR(20)"),
            ("media_metadata", "rating_consensus", "DOUBLE PRECISION"),
            ("media_metadata", "rating_source_count", "INTEGER DEFAULT 0"),
            # Curated and generated categories share one table, and the seeder
            # re-syncs on every startup. Without `source` it would treat every
            # synthesized row as a curated row that has drifted, and either
            # rewrite or orphan it on each boot.
            ("universal_categories", "source", "VARCHAR(16) DEFAULT 'curated'"),
            ("universal_categories", "name_source", "VARCHAR(16)"),
            ("universal_categories", "name_spec_hash", "VARCHAR(40)"),
            ("universal_categories", "manifest_priority", "INTEGER DEFAULT 0"),
            # Cost ledger split by billable class. The former total-only column
            # could not distinguish zero metadata from zero spend, nor expose
            # internal retries (the cause of the $58 rebuild overrun).
            ("tagging_jobs", "total_api_attempts", "INTEGER DEFAULT 0 NOT NULL"),
            ("tagging_jobs", "input_tokens", "INTEGER DEFAULT 0 NOT NULL"),
            ("tagging_jobs", "output_tokens", "INTEGER DEFAULT 0 NOT NULL"),
            ("tagging_jobs", "cached_tokens", "INTEGER DEFAULT 0 NOT NULL"),
            ("tagging_jobs", "thinking_tokens", "INTEGER DEFAULT 0 NOT NULL"),
            ("tagging_jobs", "usage_estimated", "BOOLEAN DEFAULT FALSE NOT NULL"),
            ("tagging_jobs", "pricing_basis", "VARCHAR(40)"),
        ]
        for table, column, col_type in tagging_v2_migrations:
            if engine.dialect.name != "postgresql":
                # SQLite has no DOUBLE PRECISION and no ADD COLUMN IF NOT EXISTS.
                col_type = col_type.replace("DOUBLE PRECISION", "FLOAT")
            try:
                db.execute(text(f"SELECT {column} FROM {table} LIMIT 1"))
            except Exception:
                db.rollback()
                logger.info(f"Adding missing column {table}.{column}")
                if engine.dialect.name == "postgresql":
                    db.execute(
                        text(
                            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS "
                            f"{column} {col_type}"
                        )
                    )
                else:
                    db.execute(
                        text(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
                    )
                db.commit()

        taxonomy_index_ddl = [
            "CREATE INDEX IF NOT EXISTS idx_tags_parent ON tags (parent_id)",
            "CREATE INDEX IF NOT EXISTS idx_tags_facet_active "
            "ON tags (category, is_active)",
            "CREATE INDEX IF NOT EXISTS idx_tag_aliases_tag " "ON tag_aliases (tag_id)",
            "CREATE INDEX IF NOT EXISTS idx_tag_obs_occurrences "
            "ON tag_observations (occurrences)",
            # Drives re-tag selection and the admin version breakdown.
            "CREATE INDEX IF NOT EXISTS idx_movie_tags_version "
            "ON movie_tags (media_type, tagger_version)",
            # Enrichment picks up where it stopped by scanning these.
            "CREATE INDEX IF NOT EXISTS idx_media_enrich_version "
            "ON media_metadata (enrich_version)",
            "CREATE INDEX IF NOT EXISTS idx_media_tag_attempt_version "
            "ON media_metadata (tag_attempt_version)",
            "CREATE INDEX IF NOT EXISTS idx_media_mdb_enriched "
            "ON media_metadata (mdb_enriched_at)",
        ]
        for ddl in taxonomy_index_ddl:
            try:
                db.execute(text(ddl))
                db.commit()
            except Exception as e:
                db.rollback()
                logger.warning(f"Could not create taxonomy index: {e}")

        _reset_stale_catalog_toggles(db)
        _upgrade_catalog_source_floor(db)


#: Marker recording that :func:`_reset_stale_catalog_toggles` has run.
CATALOG_TOGGLE_RESET_MARKER = "CATALOG_TOGGLE_RESET_V1"

#: Toggles that were offered by the dashboard before they were settings.
_STALE_CATALOG_TOGGLES = ("ENABLE_UNIVERSAL_CATALOGS", "ENABLE_PERSONALIZED_CATALOGS")

#: One-time migration for the production-audited source floor. Admin Center
#: saved every displayed value, so existing installs persist former defaults
#: even when the application default changes.
CATALOG_SOURCE_FLOOR_UPGRADE_MARKER = "CATALOG_SOURCE_FLOOR_V3"


def _upgrade_catalog_source_floor(db):
    """Raise former persisted defaults from two/four sources to five.

    Values other than ``2`` and ``4`` are operator choices and survive. The
    marker makes a later deliberate lower choice survive as well; this is a
    deployment migration, not a permanent clamp.
    """

    from app.models import AdminSetting

    try:
        seen = (
            db.query(AdminSetting)
            .filter(AdminSetting.key == CATALOG_SOURCE_FLOOR_UPGRADE_MARKER)
            .first()
        )
        if seen is not None:
            return
        row = (
            db.query(AdminSetting)
            .filter(AdminSetting.key == "CATALOG_MIN_RATING_SOURCES")
            .first()
        )
        if row is not None and str(row.value).strip() in {"2", "4"}:
            previous = str(row.value).strip()
            row.value = "5"
            row.updated_at = utc_now()
            logger.info(
                "Raised the persisted catalog rating-source floor from the "
                f"former default {previous} to the audited default 5"
            )
        db.add(
            AdminSetting(
                key=CATALOG_SOURCE_FLOOR_UPGRADE_MARKER,
                value=utc_now().isoformat(),
                updated_at=utc_now(),
            )
        )
        db.commit()
    except Exception as e:  # noqa: BLE001 - never block startup on a cleanup
        db.rollback()
        logger.warning(f"Could not upgrade the catalog source floor: {e}")


def _reset_stale_catalog_toggles(db):
    """Drop values written by two toggles that were wired to nothing.

    Both keys were offered in the settings tab, the README and ``.env.example``
    while being fields on no class. ``saveSettings`` sends every toggle on every
    save and ``update_settings`` persists anything non-empty -- and ``str(False)``
    is ``"False"`` -- so any install where Save Settings was ever pressed holds a
    stored value for them. ``_apply_setting``'s ``hasattr`` check then discarded
    it, which is precisely the defect that made them real settings.

    Now that they exist, replaying a stale ``False`` would switch off both
    catalog tiers on the first boot after the upgrade: every row gone from the
    web dashboard and the native home feed, and nothing in the logs to say why,
    because nothing reads a disabled feature.

    The stored value is not evidence of intent. Until a save happened the
    toggles rendered OFF whatever the state, so ``False`` means "never touched
    this" as readily as "turned this off" -- and either way the instance has
    been *serving* both tiers continuously, which is the behaviour worth
    preserving across an upgrade. Reset to the documented default; the toggle is
    one click away for anyone who does want it off.

    Guarded by a marker rather than made unconditional, so this runs once. A
    value stored deliberately *after* the upgrade is a real setting and survives.
    """
    from app.models import AdminSetting

    try:
        seen = (
            db.query(AdminSetting)
            .filter(AdminSetting.key == CATALOG_TOGGLE_RESET_MARKER)
            .first()
        )
        if seen is not None:
            return
        cleared = (
            db.query(AdminSetting)
            .filter(AdminSetting.key.in_(_STALE_CATALOG_TOGGLES))
            .delete(synchronize_session=False)
        )
        db.add(
            AdminSetting(
                key=CATALOG_TOGGLE_RESET_MARKER,
                value=utc_now().isoformat(),
                updated_at=utc_now(),
            )
        )
        db.commit()
        if cleared:
            logger.info(
                f"Cleared {cleared} catalog toggle setting(s) written before the "
                "toggles were backed by anything; both default to enabled"
            )
    except Exception as e:  # noqa: BLE001 - never block startup on a cleanup
        db.rollback()
        logger.warning(f"Could not reconcile the catalog toggles: {e}")


def _backfill_accounts(db):
    """Promote each legacy user to an Account + default profile (idempotent).

    Every pre-existing ``users`` row carried its own credentials. Here we lift
    those credentials into a new ``accounts`` row and link the user (now a
    viewing profile) to it, then backfill the account_id on passkeys, passkey
    challenges and sessions. Safe to run repeatedly: rows already linked are
    skipped.
    """
    from app.models import Account, User, PasskeyCredential, PasskeyChallenge
    from app.models import UserSession

    try:
        unlinked = db.query(User).filter(User.account_id.is_(None)).all()
        if unlinked:
            logger.info(f"Backfilling accounts for {len(unlinked)} legacy user(s)")
        for user in unlinked:
            account = Account(
                auth_source=getattr(user, "auth_source", "local") or "local",
                email=user.email,
                password_hash=user.password_hash,
                totp_secret=user.totp_secret,
                totp_enabled=bool(user.totp_enabled),
                is_admin=bool(user.is_admin),
                is_active=bool(user.is_active),
                created_at=user.created_at or utc_now(),
                last_login=user.last_login or utc_now(),
            )
            db.add(account)
            db.flush()  # assign account.id
            user.account_id = account.id
        db.commit()

        # Link credentials/sessions to the owning account via their user_id.
        db.query(PasskeyCredential).filter(
            PasskeyCredential.account_id.is_(None),
            PasskeyCredential.user_id.isnot(None),
        ).update(
            {
                PasskeyCredential.account_id: db.query(User.account_id)
                .filter(User.id == PasskeyCredential.user_id)
                .scalar_subquery()
            },
            synchronize_session=False,
        )
        db.query(PasskeyChallenge).filter(
            PasskeyChallenge.account_id.is_(None),
            PasskeyChallenge.user_id.isnot(None),
        ).update(
            {
                PasskeyChallenge.account_id: db.query(User.account_id)
                .filter(User.id == PasskeyChallenge.user_id)
                .scalar_subquery()
            },
            synchronize_session=False,
        )
        db.query(UserSession).filter(
            UserSession.account_id.is_(None),
            UserSession.user_id.isnot(None),
        ).update(
            {
                UserSession.account_id: db.query(User.account_id)
                .filter(User.id == UserSession.user_id)
                .scalar_subquery(),
                UserSession.active_profile_id: UserSession.user_id,
            },
            synchronize_session=False,
        )
        db.commit()
    except Exception as e:
        db.rollback()
        logger.warning(f"Account backfill skipped/failed: {e}")


def _reset_stale_fingerprint_jobs():
    with get_db() as db:
        try:
            from app.models import EpisodeFingerprint

            cutoff = utc_now() - timedelta(minutes=30)
            stale_rows = (
                db.query(EpisodeFingerprint)
                .filter(
                    EpisodeFingerprint.status == "processing",
                    EpisodeFingerprint.updated_at < cutoff,
                )
                .all()
            )
            for row in stale_rows:
                payload = row.fingerprint or {}
                has_usable_phase = bool(payload.get("head") or payload.get("tail"))
                row.status = "partial" if has_usable_phase else "pending"
            db.commit()
        except Exception:
            db.rollback()
