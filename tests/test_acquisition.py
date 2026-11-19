# ruff: noqa: E402
"""Acquisition: stoppable, and separable from tagging.

Two properties the crawl lacked, both of which had to exist before the pre-1970
coverage gap could be investigated at all:

* **Stoppable.** ``_raise_if_stopped()`` was called only *before* each fetch, so
  pressing Stop during a multi-hour acquisition did nothing until that whole
  media type finished. It was the one long stage of a build that could not be
  interrupted.
* **Separable.** ``main()`` tagged unconditionally, so there was no way to widen
  what the library collects without also paying Gemini for every new title and
  rebuilding every catalog row in the same run. Measuring the gap required
  buying a tagging pass.
"""

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_acquisition.db")
os.environ.setdefault("TMDB_API_KEY", "test_key")
os.environ.setdefault("GEMINI_API_KEY", "test_key")
os.environ.setdefault("MASTER_PASSWORD", "test_password")
os.environ.setdefault("SECRET_KEY", "test_secret_key_at_least_32_chars_long")
os.environ.setdefault("BASE_URL", "http://localhost:8000")
os.environ.setdefault("SKIP_API_VALIDATION", "true")

from app import database as app_database  # noqa: E402
from app.models import Base, MediaMetadata, MovieTag  # noqa: E402
from app.tmdb_client import TMDBClient  # noqa: E402

TEST_DATABASE_URL = "sqlite:///./test_acquisition.db"
engine = create_engine(TEST_DATABASE_URL, connect_args={"check_same_thread": False})
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture()
def db(monkeypatch):
    Base.metadata.create_all(bind=engine)
    session = TestingSessionLocal()

    class _Ctx:
        def __enter__(self):
            return session

        def __exit__(self, *exc):
            return False

    import workers.initial_build as build_module

    for module in (app_database, build_module):
        monkeypatch.setattr(module, "get_db", lambda: _Ctx(), raising=False)
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


def _page(ids, total_pages=50):
    return {
        "results": [{"id": i, "title": f"T{i}", "overview": ""} for i in ids],
        "total_pages": total_pages,
    }


# ---------------------------------------------------------------------------
# Stoppability
# ---------------------------------------------------------------------------
def test_a_crawl_stops_mid_pagination(db):
    """Without this, Stop was inert for the length of an entire media type."""
    client = TMDBClient(api_key="test")
    pages_fetched = {"n": 0}

    async def _request(endpoint, params=None):
        pages_fetched["n"] += 1
        page = (params or {}).get("page", 1)
        return _page(range(page * 100, page * 100 + 20))

    stop_after = 3

    with patch.object(client, "_request", side_effect=_request):
        results = asyncio.run(
            client._paginate_discover(
                "movie",
                {"sort_by": "popularity.desc"},
                set(),
                max_pages=500,
                should_stop=lambda: pages_fetched["n"] >= stop_after,
            )
        )

    assert pages_fetched["n"] == stop_after, "the crawl must halt within one page"
    assert results, "work already collected is returned, not discarded"


def test_a_crawl_with_no_stop_callable_is_unchanged(db):
    """The parameter is additive; existing callers must behave as before."""
    client = TMDBClient(api_key="test")

    async def _request(endpoint, params=None):
        return _page(range(1, 21), total_pages=1)

    with patch.object(client, "_request", side_effect=_request):
        results = asyncio.run(
            client._paginate_discover("movie", {}, set(), max_pages=500)
        )

    assert len(results) == 20


def test_a_stop_between_strategies_ends_the_whole_fetch(db):
    """Strategy boundaries are checked too, not just pages within one."""
    client = TMDBClient(api_key="test")
    calls = {"n": 0}

    async def _popular(media_type, limit=10000):
        calls["n"] += 1
        return [{"id": 1, "title": "One"}]

    async def _request(endpoint, params=None):
        calls["n"] += 1
        return _page([], total_pages=1)

    with patch.object(client, "get_popular_items", side_effect=_popular), patch.object(
        client, "_request", side_effect=_request
    ):
        items = asyncio.run(
            client.fetch_all_items("movie", limit=100000, should_stop=lambda: True)
        )

    # Strategy 1 runs (the flag is checked after it), then the run ends rather
    # than proceeding through top-rated, 57 years, gems and 15 languages.
    assert items == [{"id": 1, "title": "One"}]


# ---------------------------------------------------------------------------
# Fetch-only
# ---------------------------------------------------------------------------
def test_fetch_only_stores_metadata_and_pays_for_no_tagging(db, monkeypatch):
    """The separation that makes measuring the coverage gap affordable."""
    import workers.initial_build as build_module

    tagged = {"called": False}

    async def _never(*a, **kw):
        tagged["called"] = True
        return 0, 0, ""

    async def _fetch(media_type, limit):
        return [
            {
                "id": 1 if media_type == "movie" else 2,
                "title": "T",
                "overview": "",
                "release_date": "1947-01-01",
            }
        ]

    async def _categories(db_):
        return None

    monkeypatch.setattr(build_module, "tag_items_batch", _never)
    monkeypatch.setattr(build_module, "fetch_items", _fetch)
    monkeypatch.setattr(build_module, "create_universal_categories", _categories)
    monkeypatch.setattr(
        build_module, "generate_all_catalogs", AsyncMock(side_effect=AssertionError)
    )
    monkeypatch.setattr(build_module, "init_database", lambda: None)

    asyncio.run(build_module.main(10, 10, fetch_only=True))

    assert tagged["called"] is False, "fetch-only must not call Gemini"
    assert db.query(MediaMetadata).count() == 2, "metadata is still stored"
    assert db.query(MovieTag).count() == 0


def test_a_normal_build_still_tags(db, monkeypatch):
    """Regression guard: the flag defaults off and changes nothing."""
    import workers.initial_build as build_module

    tagged = {"called": False}

    async def _tag(db_, items, media_type, **kw):
        tagged["called"] = True
        return len(items), 0, ""

    async def _fetch(media_type, limit):
        return [{"id": 1, "title": "T", "overview": ""}]

    async def _noop(*a, **kw):
        return None

    monkeypatch.setattr(build_module, "tag_items_batch", _tag)
    monkeypatch.setattr(build_module, "fetch_items", _fetch)
    monkeypatch.setattr(build_module, "create_universal_categories", _noop)
    monkeypatch.setattr(build_module, "generate_all_catalogs", _noop)
    monkeypatch.setattr(build_module, "init_database", lambda: None)

    asyncio.run(build_module.main(10, 10))

    assert tagged["called"] is True


def test_re_running_acquisition_preserves_enrichment(db, monkeypatch):
    """The end-to-end version of the metadata_store guarantee.

    This is the property that made D18 unattemptable: acquisition writes list
    payloads, and a blind merge would have stripped imdb_id from every
    re-fetched title across a 354k-row library.
    """
    import workers.initial_build as build_module

    db.add(
        MediaMetadata(
            tmdb_id=1,
            media_type="movie",
            title="Casablanca",
            imdb_id="tt0034583",
            logo_path="/logo.png",
            enrich_version="1.0.0",
        )
    )
    db.commit()

    asyncio.run(
        build_module.store_metadata(
            db,
            [
                {
                    "id": 1,
                    "title": "Casablanca",
                    "overview": "",
                    "release_date": "1942-11-26",
                    "popularity": 42.0,
                }
            ],
            "movie",
        )
    )

    row = db.query(MediaMetadata).filter_by(tmdb_id=1).one()
    assert row.imdb_id == "tt0034583"
    assert row.logo_path == "/logo.png"
    assert row.enrich_version == "1.0.0"
    assert row.popularity == 42.0, "and fresh values still land"
