# ruff: noqa: E402
"""Tests for server-side search: filtering, ranking, and type repair."""

import os
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_search_ranking.db")
os.environ.setdefault("TMDB_API_KEY", "test_key")
os.environ.setdefault("GEMINI_API_KEY", "test_key")
os.environ.setdefault("MASTER_PASSWORD", "test_password")
os.environ.setdefault("SECRET_KEY", "test_secret_key_at_least_32_chars_long")
os.environ.setdefault("BASE_URL", "http://localhost:8000")
os.environ.setdefault("SKIP_API_VALIDATION", "true")

import app.main as main  # noqa: E402
from app.auth import create_user_session  # noqa: E402
from app.database import get_db_dependency  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    PasskeyCredential,
    Account,
    Base,
    ImdbResolution,
    MediaMetadata,
    User,
)

engine = create_engine(
    "sqlite:///./test_search_ranking.db",
    connect_args={"check_same_thread": False},
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def _bearer(user_key):
    """Profile credential header for the native /api/v2 API."""
    return {"Authorization": f"Bearer {user_key}"}


def _clear_caches():
    main._user_cache.clear()
    main._meta_cache.clear()


def make_client():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = TestingSessionLocal()
    _clear_caches()

    def override_get_db():
        try:
            yield db
        finally:
            pass

    app.dependency_overrides[get_db_dependency] = override_get_db
    return TestClient(app), db


def cleanup(db):
    db.close()
    app.dependency_overrides.clear()
    _clear_caches()
    Base.metadata.drop_all(bind=engine)


@contextmanager
def use_test_db(db):
    """Stand-in for app.main.get_db: commit-on-success, no close (test keeps it)."""
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise


def _login(client, db):
    account = Account(
        auth_source="local",
        email="viewer@example.com",
        password_hash="x",
        is_active=True,
        totp_enabled=True,
    )
    db.add(account)
    db.commit()
    # A finished passkey + TOTP enrollment, so the session clears the gate.
    db.add(
        PasskeyCredential(
            account_id=account.id,
            credential_id="test-credential",
            public_key="test-public-key",
        )
    )
    db.commit()
    user = User(
        user_key="web-user-key",
        account_id=account.id,
        auth_source="local",
        display_name="Viewer",
        is_active=True,
    )
    db.add(user)
    db.commit()
    token = create_user_session(account.id, db, active_profile_id=user.id)
    client.cookies.set("user_session", token)
    return user


# --------------------------------------------------------------------------- #
# Fixtures / builders
# --------------------------------------------------------------------------- #


def result_item(
    imdb_id,
    title,
    *,
    media_type="movie",
    poster="/poster.jpg",
    year="2010",
    rating=8.5,
    genres=None,
    vote_count=None,
):
    """An IMDb-keyed search item in the internal shape the ranker reads."""
    return {
        "tmdb_id": None,
        "imdb_id": imdb_id,
        "media_type": media_type,
        "title": title,
        "poster": poster,
        "backdrop": None,
        "logo": None,
        "year": year,
        "description": f"{title} description",
        "rating": rating,
        "genres": genres if genres is not None else ["Action"],
        "vote_count": vote_count,
    }


def db_item(
    title,
    *,
    tmdb_id=1,
    imdb_id=None,
    media_type="movie",
    rating=None,
    popularity=None,
    year="",
    poster=None,
    genres=None,
    quality_score=None,
    vote_count=None,
    collection_id=None,
    collection_name=None,
):
    """A DB-sourced internal item (as _search_media_multi produces, minus the ORM row)."""
    return {
        "tmdb_id": tmdb_id,
        "media_type": media_type,
        "title": title,
        "poster": poster,
        "backdrop": None,
        "year": year,
        "description": None,
        "rating": rating,
        "genres": genres if genres is not None else [],
        "imdb_id": imdb_id,
        "logo": None,
        "popularity": popularity,
        "vote_count": vote_count,
        "quality_score": quality_score,
        "collection_id": collection_id,
        "collection_name": collection_name,
        "_row": None,
    }


def seed_movie(db, **kw):
    row = MediaMetadata(
        tmdb_id=kw["tmdb_id"],
        media_type=kw.get("media_type", "movie"),
        title=kw["title"],
        overview=kw.get(
            "overview",
            "A substantive synopsis with enough evidence for quality-aware discovery search.",
        ),
        release_date=kw.get("release_date", "2010-01-01"),
        genres=kw.get("genres", ["Action"]),
        poster_path=kw.get("poster_path", "/db_poster.jpg"),
        imdb_id=kw.get("imdb_id"),
        vote_average=kw.get("vote_average", 8.0),
        vote_count=kw.get("vote_count", 1000),
        rating_consensus=kw.get("rating_consensus"),
        popularity=kw.get("popularity", 50.0),
        raw_data=kw.get("raw_data"),
        number_of_seasons=kw.get("number_of_seasons"),
        number_of_episodes=kw.get("number_of_episodes"),
        next_episode_air_date=kw.get("next_episode_air_date"),
    )
    db.add(row)
    db.commit()
    return row


# --------------------------------------------------------------------------- #
# Pure: normalization + ranking
# --------------------------------------------------------------------------- #


def test_normalize_strips_leading_the_and_punctuation():
    assert main._normalize_search_text("The Office!") == "office"
    assert (
        main._normalize_search_text("  Spider-Man: No Way Home ")
        == "spider man no way home"
    )
    # "the" is only stripped as a leading article, not inside a word
    assert main._normalize_search_text("Theater") == "theater"


def test_extract_query_year():
    text, year = main._extract_query_year("batman 2022")
    assert year == 2022
    assert "2022" not in text
    assert main._normalize_search_text(text) == "batman"
    assert main._extract_query_year("batman") == ("batman", None)


def test_explicit_filter_drops_imdb_adult_genre():
    """IMDb-derived genres carry a bare "Adult" genre; it must be filtered."""
    items = [
        result_item("tt1", "Nova", genres=["Drama"]),
        result_item("tt2", "Nova XXX", genres=["Adult"]),
    ]

    kept = main._drop_explicit_results(items)

    assert [item["imdb_id"] for item in kept] == ["tt1"]


def test_explicit_filter_drops_provider_flag_and_xxx_title_marker():
    flagged = result_item("tt1", "Opaque Catalog Title", genres=["Drama"])
    flagged["adult"] = True
    items = [
        flagged,
        result_item("tt2", "LateNightXXXVideos", genres=["Drama"]),
        result_item("tt3", "Explicit Porn Collection", genres=["Drama"]),
    ]

    assert main._drop_explicit_results(items) == []


def test_explicit_filter_keeps_mainstream_xxx_franchise_titles():
    items = [
        result_item("tt1", "xXx", genres=["Action", "Thriller"]),
        result_item("tt2", "xXx: State of the Union", genres=["Action"]),
        result_item("tt3", "xXx: Return of Xander Cage", genres=["Action"]),
    ]

    assert main._drop_explicit_results(items) == items


def test_explicit_filter_keeps_adult_animation():
    """ "Adult" is an exact-match rule, not a substring one.

    A substring rule would remove every adult-animation title from search --
    the precise over-filtering the genre blocklist has to avoid.
    """
    items = [result_item("tt1", "Nova", genres=["Adult Animation", "Comedy"])]

    assert main._drop_explicit_results(items) == items


def test_explicit_filter_matches_compound_porn_genres():
    items = [
        result_item("tt1", "Nova", genres=["Softcore Porn"]),
        result_item("tt2", "Nova", genres=["Erotic"]),
        result_item("tt3", "Nova", genres=["Hentai"]),
    ]

    assert main._drop_explicit_results(items) == []


def test_explicit_filter_ignores_synopsis_and_keeps_mature_drama():
    """Mature themes in a plot are not pornography.

    "The Girl Next Door" is about a former adult-film star and "Boogie Nights"
    is set in the porn industry; both must survive.
    """
    items = [
        result_item("tt1", "The Girl Next Door", genres=["Comedy", "Romance"]),
        result_item("tt2", "Boogie Nights", genres=["Drama"]),
    ]
    items[0]["description"] = "A former adult film star moves in next door."
    items[1]["description"] = "The rise and fall of a porn industry star."

    assert main._drop_explicit_results(items) == items


def test_explicit_filter_applies_to_our_own_rows_too():
    items = [db_item("Nova", tmdb_id=1, genres=["Drama"])]
    items.append(db_item("Emmanuelle", tmdb_id=2, genres=["Drama"]))

    kept = main._drop_explicit_results(items)

    assert [item["tmdb_id"] for item in kept] == [1]


def test_rank_broad_query_allows_mainstream_whole_word_to_beat_obscure_exact():
    items = [
        result_item("tt1", "Zebra Heist", vote_count=20_000),
        result_item("tt2", "Heist", vote_count=3, rating=1.0),
    ]
    ranked = main._rank_search_results(items, "heist")
    assert ranked[0]["imdb_id"] == "tt1"


def test_rank_specific_exact_title_still_dominates_popularity():
    items = [
        result_item("tt1", "Zebra Great Heist", vote_count=20_000),
        result_item("tt2", "Great Heist", vote_count=3, rating=1.0),
    ]

    ranked = main._rank_search_results(items, "great heist")

    assert ranked[0]["imdb_id"] == "tt2"


def test_rank_leading_the_equivalence():
    items = [
        result_item("tt1", "Office Space", vote_count=5_000),  # prefix
        # exact after stripping "the"
        result_item("tt2", "The Office", vote_count=5_000),
    ]
    ranked = main._rank_search_results(items, "office")
    assert ranked[0]["imdb_id"] == "tt2"


def test_rank_year_boost():
    items = [
        result_item("tt_old", "Halloween", vote_count=5_000, year="1978"),
        result_item("tt_new", "Halloween", vote_count=5_000, year="2018"),
    ]
    ranked = main._rank_search_results(items, "halloween 2018")
    assert ranked[0]["imdb_id"] == "tt_new"


def test_rank_db_pop_fallback_for_our_only_items():
    items = [
        db_item("Nova", tmdb_id=1, popularity=5.0),
        db_item("Nova", tmdb_id=2, popularity=400.0),
    ]
    ranked = main._rank_search_results(items, "nova")
    assert ranked[0]["tmdb_id"] == 2  # higher DB popularity ranks first within tier


def test_rank_recognition_leads_within_tier():
    """Recognition -- lifetime vote_count -- orders equally-relevant results."""
    items = [
        db_item("Nova", tmdb_id=1, vote_count=20, quality_score=0.80),
        db_item("Nova", tmdb_id=2, vote_count=12000, quality_score=0.80),
    ]

    ranked = main._rank_search_results(items, "nova")

    assert ranked[0]["tmdb_id"] == 2


def test_rank_trending_churn_does_not_outrank_an_established_title():
    """The regression this weighting exists to prevent.

    TMDB `popularity` is recency-weighted activity, so a low-budget new release
    can sit at 900 while an established film sits at 35. Ranking on it put this
    week's churn on top. `vote_count` is what a person means by "popular", and
    it is also the ONLY signal that separates the two: `_quality_value` is
    Bayesian against a 6.5/250 prior, so the 40-vote title scores ~6.3/10 --
    statistically indistinguishable from a decent film.
    """
    items = [
        db_item(
            "Nova Churn",
            tmdb_id=1,
            popularity=900.0,
            vote_count=40,
            quality_score=0.63,
            poster="/a.jpg",
        ),
        db_item(
            "Nova Classic",
            tmdb_id=2,
            popularity=35.0,
            vote_count=20000,
            quality_score=0.87,
            poster="/b.jpg",
        ),
    ]

    ranked = main._rank_search_results(items, "nova")

    assert ranked[0]["tmdb_id"] == 2


def test_rank_quality_orders_equally_recognized_titles():
    items = [
        db_item("Nova", tmdb_id=1, vote_count=5000, quality_score=0.45),
        db_item("Nova", tmdb_id=2, vote_count=5000, quality_score=0.95),
    ]

    ranked = main._rank_search_results(items, "nova")

    assert ranked[0]["tmdb_id"] == 2


def test_rank_buzz_cannot_overturn_recognition_plus_quality():
    """Buzz is a nudge. Bound it arithmetically so retuning cannot re-break this."""
    assert main._W_BUZZ < main._W_QUALITY
    assert main._W_BUZZ < main._W_RECOGNITION
    assert main._W_BUZZ * 4 <= main._W_RECOGNITION


def test_rank_tier_gap_exceeds_every_other_signal_combined():
    """The invariant the old weights violated: relevance cannot be outweighed.

    Gaps were 100 while quality + popularity + year summed to 115, so a weaker
    match could and did displace a stronger one. Assert it arithmetically rather
    than by example, so re-tuning any weight fails here instead of in production.
    """
    best_case = main._W_RECOGNITION + main._W_QUALITY + main._W_BUZZ + main._YEAR_BOOST
    worst_case = main._P_NO_ARTWORK
    smallest_gap = main._TIER_EXACT - main._TIER_PREFIX

    assert best_case + worst_case < smallest_gap


def test_rank_demotes_rather_than_hides_a_thin_obscure_title():
    """A no-artwork, no-votes title sinks below a strong one -- but is still there.

    The whole point of demoting instead of filtering: searching the obscure
    title's own name puts it first (tier dominance), while searching a generic
    word leaves it below the titles people mean.
    """
    items = [
        db_item(
            "Nova Rising",
            tmdb_id=1,
            popularity=300.0,
            quality_score=0.9,
            vote_count=8000,
            poster="/p.jpg",
        ),
        db_item(
            "Nova Obscura",
            tmdb_id=2,
            popularity=0.4,
            quality_score=0.31,
            vote_count=2,
            poster=None,
        ),
    ]

    ranked = main._rank_search_results(items, "nova")
    assert [item["tmdb_id"] for item in ranked] == [1, 2]

    # ...and it is first the moment it is what was actually typed.
    exact = main._rank_search_results(items, "nova obscura")
    assert exact[0]["tmdb_id"] == 2


def test_rank_keeps_non_matches_while_the_query_is_underserved():
    """Recall protection: a sparse query keeps even non-matching candidates."""
    items = [
        db_item("Inception", tmdb_id=1, popularity=100.0),
        db_item("Tenet", tmdb_id=2, popularity=90.0),
    ]

    ranked = main._rank_search_results(items, "inception")

    assert [item["tmdb_id"] for item in ranked] == [1, 2]


def test_rank_drops_non_matches_once_the_query_is_well_served():
    items = [
        db_item(f"Nova Part {index}", tmdb_id=index, popularity=100.0)
        for index in range(1, main._MIN_REAL_MATCHES + 1)
    ]
    items.append(db_item("Unrelated Film", tmdb_id=99, popularity=5000.0))

    ranked = main._rank_search_results(items, "nova")

    assert 99 not in [item["tmdb_id"] for item in ranked]
    assert len(ranked) == main._MIN_REAL_MATCHES


def test_rank_groups_a_franchise_in_release_order():
    """A collection is emitted as one contiguous block, oldest first.

    Anchored where its best-ranked member landed, so the franchise takes the
    position it earned and its films no longer interleave with other titles.
    """
    items = [
        db_item(
            "Nova III",
            tmdb_id=3,
            year="2011",
            popularity=400.0,
            collection_id=77,
            collection_name="Nova Collection",
        ),
        db_item("Nova Unrelated", tmdb_id=9, year="2015", popularity=380.0),
        db_item(
            "Nova I",
            tmdb_id=1,
            year="2005",
            popularity=90.0,
            collection_id=77,
            collection_name="Nova Collection",
        ),
        db_item(
            "Nova II",
            tmdb_id=2,
            year="2008",
            popularity=95.0,
            collection_id=77,
            collection_name="Nova Collection",
        ),
    ]

    ranked = main._rank_search_results(items, "nova")

    assert [item["tmdb_id"] for item in ranked] == [1, 2, 3, 9]


def test_rank_franchise_block_leads_with_the_exactly_named_film():
    items = [
        db_item(
            "Nova I",
            tmdb_id=1,
            year="2005",
            popularity=400.0,
            collection_id=77,
            collection_name="Nova Collection",
        ),
        db_item(
            "Nova II",
            tmdb_id=2,
            year="2008",
            popularity=90.0,
            collection_id=77,
            collection_name="Nova Collection",
        ),
        db_item(
            "Nova III",
            tmdb_id=3,
            year="2011",
            popularity=95.0,
            collection_id=77,
            collection_name="Nova Collection",
        ),
    ]

    ranked = main._rank_search_results(items, "nova ii")

    assert [item["tmdb_id"] for item in ranked] == [2, 1, 3]


def test_rank_leaves_uncollected_results_untouched():
    """Rows with no collection_id (TV, standalone, not-yet-backfilled) are a no-op."""
    items = [
        db_item("Nova Alpha", tmdb_id=1, popularity=400.0),
        db_item("Nova Beta", tmdb_id=2, popularity=90.0),
    ]

    ranked = main._rank_search_results(items, "nova")

    assert [item["tmdb_id"] for item in ranked] == [1, 2]


# --------------------------------------------------------------------------- #
# Web dashboard search
# --------------------------------------------------------------------------- #


def test_web_search_schema_unchanged_for_db_items():
    client, db = make_client()
    try:
        _login(client, db)
        row = seed_movie(db, tmdb_id=27205, title="Inception", imdb_id="tt1375666")
        control = main._serialize_media_item(
            tmdb_id=row.tmdb_id, media_type=row.media_type, title=row.title, meta=row
        )
        resp = client.get("/account/api/search?q=inception")
        items = resp.json()["items"]
        db_entry = [i for i in items if i["tmdb_id"] == 27205][0]
        # Search adds only the v2 ownership flag on top of the shared card shape.
        assert set(db_entry.keys()) == set(control.keys()) | {"availability"}
    finally:
        cleanup(db)


def test_web_search_uses_year_for_ranking_not_literal_sql_match():
    client, db = make_client()
    try:
        _login(client, db)
        seed_movie(db, tmdb_id=1, title="Dune", release_date="1984-12-14")
        seed_movie(db, tmdb_id=2, title="Dune", release_date="2021-10-22")

        response = client.get("/account/api/search?q=dune%202021")

        assert response.status_code == 200
        assert [item["tmdb_id"] for item in response.json()["items"][:2]] == [2, 1]
    finally:
        cleanup(db)


def test_web_search_keeps_known_low_rated_title_but_ranks_quality_first():
    client, db = make_client()
    try:
        _login(client, db)
        seed_movie(
            db,
            tmdb_id=1,
            title="Nova Quality",
            vote_average=8.2,
            vote_count=5000,
        )
        seed_movie(
            db,
            tmdb_id=2,
            title="Nova Junk",
            vote_average=5.0,
            vote_count=5000,
        )

        response = client.get("/account/api/search?q=nova")

        assert response.status_code == 200
        assert [item["tmdb_id"] for item in response.json()["items"]] == [1, 2]
    finally:
        cleanup(db)


def test_web_search_exact_title_does_not_require_catalog_eligibility():
    client, db = make_client()
    try:
        _login(client, db)
        seed_movie(
            db,
            tmdb_id=100757,
            title="Outer Banks",
            overview="",
            vote_average=0.0,
            vote_count=0,
            popularity=0.0,
        )

        response = client.get("/account/api/search?q=outer%20banks")

        assert response.status_code == 200
        assert [item["title"] for item in response.json()["items"]] == ["Outer Banks"]
    finally:
        cleanup(db)


def test_exact_search_keeps_home_suppressed_mockbuster():
    client, db = make_client()
    try:
        _login(client, db)
        seed_movie(
            db,
            tmdb_id=1698863,
            title="The Odyssey",
            imdb_id="tt41605854",
            release_date="2026-07-03",
            vote_average=2.0,
            vote_count=10,
        )

        response = client.get("/account/api/search?q=the%20odyssey")

        assert response.status_code == 200
        assert [item["tmdb_id"] for item in response.json()["items"]] == [1698863]
        assert response.json()["items"][0]["imdb_id"] == "tt41605854"
    finally:
        cleanup(db)


def test_native_search_preserves_tv_media_type():
    client, db = make_client()
    try:
        user = _login(client, db)
        seed_movie(
            db,
            tmdb_id=2316,
            media_type="tv",
            title="The Office",
            release_date="2005-03-24",
        )

        response = client.get(
            "/api/v2/search?q=the%20office", headers=_bearer(user.user_key)
        )

        assert response.status_code == 200
        assert response.json()["items"][0]["media_type"] == "tv"
    finally:
        cleanup(db)


def test_native_search_never_downgrades_tv_from_stale_imdb_resolution():
    client, db = make_client()
    try:
        user = _login(client, db)
        seed_movie(
            db,
            tmdb_id=1399,
            media_type="tv",
            title="Game of Thrones",
            imdb_id="tt0944947",
            release_date="2011-04-17",
            number_of_seasons=8,
        )
        db.add(
            ImdbResolution(
                imdb_id="tt0944947",
                tmdb_id=1399,
                media_type="movie",
                status="found",
            )
        )
        db.commit()

        response = client.get(
            "/api/v2/search?q=game%20of%20thrones", headers=_bearer(user.user_key)
        )

        assert response.status_code == 200
        assert response.json()["items"][0]["media_type"] == "tv"
        assert response.json()["items"][0]["content_type"] == "series"
    finally:
        cleanup(db)


def test_native_search_scopes_imdb_resolution_to_same_tmdb_identity():
    client, db = make_client()
    try:
        user = _login(client, db)
        seed_movie(
            db,
            tmdb_id=10,
            media_type="movie",
            title="Identity Collision",
            imdb_id="tt0010010",
            raw_data=None,
        )
        db.add(
            ImdbResolution(
                imdb_id="tt0010010",
                tmdb_id=999,
                media_type="tv",
                status="found",
            )
        )
        db.commit()

        response = client.get(
            "/api/v2/search?q=identity%20collision", headers=_bearer(user.user_key)
        )

        assert response.status_code == 200
        assert response.json()["items"][0]["media_type"] == "movie"
    finally:
        cleanup(db)


def test_native_search_repairs_outer_banks_from_series_detail_evidence():
    client, db = make_client()
    try:
        user = _login(client, db)
        seed_movie(
            db,
            tmdb_id=100757,
            media_type="movie",
            title="Outer Banks",
            imdb_id="tt10293938",
            release_date="2020-04-15",
            raw_data={
                "id": 100757,
                "name": "Outer Banks",
                "first_air_date": "2020-04-15",
                "seasons": [{"season_number": 1}],
            },
            number_of_seasons=4,
            number_of_episodes=40,
        )

        response = client.get(
            "/api/v2/search?q=outer%20banks", headers=_bearer(user.user_key)
        )

        assert response.status_code == 200
        items = response.json()["items"]
        assert len(items) == 1
        assert items[0]["title"] == "Outer Banks"
        assert items[0]["media_type"] == "tv"
        assert items[0]["content_type"] == "series"
    finally:
        cleanup(db)


def test_details_type_repair_protects_clients_with_stale_movie_cards():
    _client, db = make_client()
    try:
        seed_movie(
            db,
            tmdb_id=100757,
            media_type="movie",
            title="Outer Banks",
            imdb_id="tt10293938",
            raw_data={
                "id": 100757,
                "name": "Outer Banks",
                "first_air_date": "2020-04-15",
                "seasons": [{"season_number": 1}],
            },
            number_of_seasons=4,
        )

        assert main._stored_detail_media_type(db, 100757, "movie") == "tv"
    finally:
        cleanup(db)


def test_details_type_repair_uses_only_opposite_stored_namespace():
    _client, db = make_client()
    try:
        seed_movie(
            db,
            tmdb_id=100757,
            media_type="tv",
            title="Outer Banks",
            imdb_id="tt10293938",
            raw_data=None,
        )

        assert main._stored_detail_media_type(db, 100757, "movie") == "tv"
    finally:
        cleanup(db)


def test_details_type_repair_never_downgrades_a_stored_tv_row():
    """A stored TV row is the positive evidence for the TV namespace.

    Downgrading it deletes the whole episode list — the seasons build is skipped
    and the payload then validates as a complete *movie* snapshot — so no stored
    fact is strong enough to justify it. `_search_row_media_type` short-circuits
    on `resolved_type` above every TV signal, which is how a stale or mismatched
    IMDb resolution used to win.
    """
    _client, db = make_client()
    try:
        seed_movie(
            db,
            tmdb_id=1399,
            media_type="tv",
            title="Game of Thrones",
            imdb_id="tt0944947",
            raw_data={"id": 1399, "name": "Game of Thrones"},
            number_of_seasons=8,
        )
        db.add(
            ImdbResolution(
                imdb_id="tt0944947",
                tmdb_id=1399,
                media_type="movie",
                status="found",
            )
        )
        db.commit()

        assert main._stored_detail_media_type(db, 1399, "tv") == "tv"
    finally:
        cleanup(db)


def test_details_type_repair_does_not_borrow_the_movie_namespace_for_tv():
    """TMDB's movie and TV id spaces are disjoint, so a collision proves nothing.

    "No stored TV row" is the ordinary cold-cache state for a series whose details
    have never been built; it must not resolve to the movie that happens to share
    the integer.
    """
    _client, db = make_client()
    try:
        seed_movie(
            db,
            tmdb_id=1399,
            media_type="movie",
            title="Some Unrelated Film",
            imdb_id="tt1111111",
            raw_data=None,
        )

        assert main._stored_detail_media_type(db, 1399, "tv") == "tv"
    finally:
        cleanup(db)


def test_imdb_resolution_for_another_tmdb_id_does_not_repair_this_one():
    """`imdb_id` is the resolutions table's primary key and `tmdb_id` is nullable.

    An unscoped lookup could therefore answer with a different work's media type.
    """
    _client, db = make_client()
    try:
        seed_movie(
            db,
            tmdb_id=100757,
            media_type="movie",
            title="Outer Banks",
            imdb_id="tt10293938",
            raw_data={
                "id": 100757,
                "name": "Outer Banks",
                "first_air_date": "2020-04-15",
                "seasons": [{"season_number": 1}],
            },
            number_of_seasons=4,
        )
        # Resolution for a *different* tmdb_id. It must not be consulted, so the
        # row's own TV evidence still wins.
        db.add(
            ImdbResolution(
                imdb_id="tt10293938",
                tmdb_id=999999,
                media_type="movie",
                status="found",
            )
        )
        db.commit()

        assert main._stored_detail_media_type(db, 100757, "movie") == "tv"
    finally:
        cleanup(db)


def test_details_type_repair_reports_the_branch_that_decided():
    _client, db = make_client()
    try:
        seed_movie(
            db,
            tmdb_id=100757,
            media_type="tv",
            title="Outer Banks",
            imdb_id="tt10293938",
            raw_data=None,
        )

        assert main._stored_detail_media_type_with_reason(db, 100757, "movie") == (
            "tv",
            "opposite_namespace",
        )
        assert main._stored_detail_media_type_with_reason(db, 100757, "tv") == (
            "tv",
            "requested_row",
        )
        assert main._stored_detail_media_type_with_reason(db, 424242, "tv") == (
            "tv",
            "requested",
        )
    finally:
        cleanup(db)


# --------------------------------------------------------------------------- #
# Web details: imdb -> tmdb resolution
# --------------------------------------------------------------------------- #


def test_details_resolves_imdb_only_and_caches():
    client, db = make_client()
    try:
        _login(client, db)
        find = AsyncMock(return_value={"movie_results": [{"id": 27205}]})
        movie = AsyncMock(
            return_value={"id": 27205, "title": "Inception", "genres": []}
        )
        similar = AsyncMock(return_value={"results": []})
        with patch("app.main.get_db", side_effect=lambda: use_test_db(db)), patch(
            "app.tmdb_client.tmdb_client.find_by_imdb_id", find
        ), patch("app.tmdb_client.tmdb_client.get_movie", movie), patch(
            "app.tmdb_client.tmdb_client.get_similar_movies", similar
        ):
            r1 = client.get("/account/api/details?imdb_id=tt1375666")
            r2 = client.get("/account/api/details?imdb_id=tt1375666")
        assert r1.status_code == 200
        assert r2.status_code == 200
        find.assert_awaited_once()  # mapping cached after the first resolve
    finally:
        cleanup(db)


def test_details_imdb_unresolvable_404_and_negative_cache():
    client, db = make_client()
    try:
        _login(client, db)
        find = AsyncMock(
            return_value={
                "movie_results": [],
                "tv_results": [],
                "tv_episode_results": [],
            }
        )
        with patch("app.main.get_db", side_effect=lambda: use_test_db(db)), patch(
            "app.tmdb_client.tmdb_client.find_by_imdb_id", find
        ):
            r1 = client.get("/account/api/details?imdb_id=tt0000000")
            r2 = client.get("/account/api/details?imdb_id=tt0000000")
        assert r1.status_code == 404
        assert r2.status_code == 404
        # The negative is cached persistently (ImdbResolution) so we don't re-hit TMDB.
        find.assert_awaited_once()
    finally:
        cleanup(db)


def test_details_tmdb_path_unchanged():
    client, db = make_client()
    try:
        _login(client, db)
        find = AsyncMock()
        movie = AsyncMock(return_value={"id": 550, "title": "Fight Club", "genres": []})
        similar = AsyncMock(return_value={"results": []})
        with patch("app.tmdb_client.tmdb_client.find_by_imdb_id", find), patch(
            "app.tmdb_client.tmdb_client.get_movie", movie
        ), patch("app.tmdb_client.tmdb_client.get_similar_movies", similar):
            resp = client.get("/account/api/details?tmdb_id=550&media_type=movie")
        assert resp.status_code == 200
        find.assert_not_awaited()  # tmdb path never resolves via /find
    finally:
        cleanup(db)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
