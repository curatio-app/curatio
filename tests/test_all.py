"""
Comprehensive test suite for Curatio.

Tests all major components: tagging, catalogs, API endpoints.
"""

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from fastapi.testclient import TestClient

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Set test environment variables before importing app modules
os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")
os.environ.setdefault("TMDB_API_KEY", "test_key")
os.environ.setdefault("GEMINI_API_KEY", "test_key")
os.environ.setdefault("MASTER_PASSWORD", "test_password")
os.environ.setdefault("SECRET_KEY", "test_secret_key_at_least_32_chars_long")
os.environ.setdefault("BASE_URL", "http://localhost:8000")
os.environ.setdefault("SKIP_API_VALIDATION", "true")

from app.main import app  # noqa: E402
from app.database import get_db_dependency  # noqa: E402
from app.models import (  # noqa: E402
    Base,
    Tag,
    MovieTag,
    UniversalCategory,
    MediaMetadata,
    SegmentMarker,
)
from app.gemini_client import GeminiTaggingEngine  # noqa: E402
from app.catalog_generator import CatalogGenerator  # noqa: E402

# Test database
TEST_DATABASE_URL = "sqlite:///./test.db"
engine = create_engine(TEST_DATABASE_URL, connect_args={"check_same_thread": False})
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture(scope="function")
def db():
    """Create test database for each test."""
    Base.metadata.create_all(bind=engine)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


def seed_relevance(session):
    """Populate IDF weights so ranking queries have something to rank on.

    Catalog ranking moved from ``avg(confidence)`` to ``SUM(weight_norm)``, and
    ``weight_norm`` is computed by ``relevance.refresh_tag_stats`` rather than
    written at tag-insert time. A fixture that inserts ``MovieTag`` rows
    directly therefore leaves them unweighted, and the ranking query excludes
    unweighted rows deliberately -- scoring them 0 would silently bury every
    title a nightly job added without refreshing stats.

    Call this after adding tags and before generating a catalog.
    """
    from app.relevance import refresh_tag_stats

    refresh_tag_stats(session)
    session.commit()


@pytest.fixture
def client(db):
    """FastAPI test client with test database."""

    def override_get_db():
        try:
            yield db
        finally:
            pass

    app.dependency_overrides[get_db_dependency] = override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()


# =============================================================================
# Tagging Tests
# =============================================================================


@pytest.mark.asyncio
async def test_gemini_tagging_single_movie():
    """Test tagging a single movie with Gemini."""
    response_text = json.dumps(
        {
            "items": [
                {
                    "tmdb_id": 78,
                    "tags": {"Sci-Fi": 0.95, "Neo-Noir": 0.9, "Cyberpunk": 0.85},
                }
            ]
        }
    )

    async def _post(_self, _prompt, **_constraints):
        return {
            "candidates": [{"content": {"parts": [{"text": response_text}]}}],
            "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 10},
        }

    with patch.object(GeminiTaggingEngine, "_post", _post):

        tagging_engine = GeminiTaggingEngine()

        test_movie = {
            "tmdb_id": 78,
            "media_type": "movie",
            "title": "Blade Runner",
            "overview": (
                "A cyberpunk neo-noir in which a blade runner must pursue and "
                "terminate four replicants."
            ),
            "genres": ["Science Fiction", "Thriller"],
            "release_date": "1982-06-25",
        }

        result = await tagging_engine.tag_items([test_movie])

    assert len(result) > 0
    assert result[0]["tmdb_id"] == 78
    assert "tags" in result[0]
    assert isinstance(result[0]["tags"], dict)

    tags = result[0]["tags"]
    assert any(tag in tags for tag in ["Sci-Fi", "Neo-Noir", "Cyberpunk"])


@pytest.mark.asyncio
async def test_gemini_tagging_batch():
    """Test tagging multiple movies in batch."""
    response_text = json.dumps(
        {
            "items": [
                {"tmdb_id": 78, "tags": {"Sci-Fi": 0.95}},
                {"tmdb_id": 603, "tags": {"Action": 0.9, "Sci-Fi": 0.85}},
            ]
        }
    )

    async def _post(_self, _prompt, **_constraints):
        return {
            "candidates": [{"content": {"parts": [{"text": response_text}]}}],
            "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 10},
        }

    with patch.object(GeminiTaggingEngine, "_post", _post):

        tagging_engine = GeminiTaggingEngine()

        test_movies = [
            {
                "tmdb_id": 78,
                "media_type": "movie",
                "title": "Blade Runner",
                "overview": "Sci-fi noir about replicants...",
                "genres": ["Science Fiction"],
                "release_date": "1982-06-25",
            },
            {
                "tmdb_id": 603,
                "media_type": "movie",
                "title": "The Matrix",
                "overview": "Reality is not what it seems...",
                "genres": ["Action", "Science Fiction"],
                "release_date": "1999-03-31",
            },
        ]

        results = await tagging_engine.tag_items(test_movies)

    assert len(results) == 2
    assert all("tags" in r for r in results)


def test_tag_confidence_validation():
    """Confidence coming back from Gemini is clamped into [0.0, 1.0].

    This previously asserted ``0.0 <= 0.85 <= 1.0`` against a local literal,
    which exercised nothing. It now drives the real clamp in app.tag_validation.
    """
    from app.tag_validation import validate_item_tags

    result = validate_item_tags(
        1,
        "movie",
        [["Heist", 1.7], ["Dark", 0.9], ["Detective", "not a number"]],
        shortlist_names=["Heist", "Dark", "Detective"],
    )
    scores = dict(result.rows)

    assert scores["Heist"] == 1.0, "over-range confidence must clamp to 1.0"
    assert scores["Dark"] == 0.9, "in-range confidence passes through"
    assert "Detective" not in scores, "unparseable confidence must be rejected"
    assert all(0.0 <= c <= 1.0 for c in scores.values())


# =============================================================================
# Database Tests
# =============================================================================


def test_create_tags(db):
    """Test creating tags in database."""
    tag = Tag(name="Dark", category="mood")
    db.add(tag)
    db.commit()

    retrieved = db.query(Tag).filter(Tag.name == "Dark").first()
    assert retrieved is not None
    assert retrieved.name == "Dark"
    assert retrieved.category == "mood"


def test_create_movie_tag(db):
    """Test creating movie-tag association."""
    # Create tag
    tag = Tag(name="Cyberpunk", category="style")
    db.add(tag)
    db.commit()

    # Create movie tag
    movie_tag = MovieTag(tmdb_id=78, tag_id=tag.id, confidence=0.95, media_type="movie")
    db.add(movie_tag)
    db.commit()

    # Retrieve
    retrieved = db.query(MovieTag).filter(MovieTag.tmdb_id == 78).first()
    assert retrieved is not None
    assert retrieved.confidence == 0.95
    assert retrieved.tag.name == "Cyberpunk"


def test_create_universal_category(db):
    """Test creating universal category."""
    category = UniversalCategory(
        id="test-category",
        name="Test Category",
        tier=1,
        sort_order=1,
        media_type="movie",
        tag_formula={"required": ["Dark", "Crime"], "min_required": 2},
    )
    db.add(category)
    db.commit()

    retrieved = (
        db.query(UniversalCategory)
        .filter(UniversalCategory.id == "test-category")
        .first()
    )

    assert retrieved is not None
    assert retrieved.name == "Test Category"
    assert "required" in retrieved.tag_formula


# =============================================================================
# Catalog Generator Tests
# =============================================================================


def test_catalog_generator_setup(db):
    """Test catalog generator initialization."""
    generator = CatalogGenerator(db)
    assert generator.db is not None


def test_generate_universal_catalog(db):
    """Test generating a universal catalog from tags."""
    # Create tags
    dark_tag = Tag(name="Dark", category="mood")
    crime_tag = Tag(name="Crime", category="genre")
    db.add_all([dark_tag, crime_tag])
    db.commit()

    # Create test movies
    movies = [
        MediaMetadata(
            tmdb_id=1,
            media_type="movie",
            title="Dark Crime Movie 1",
            overview="A complete synopsis with enough evidence for semantic tagging.",
            popularity=100.0,
            vote_average=8.0,
            vote_count=1500,
        ),
        MediaMetadata(
            tmdb_id=2,
            media_type="movie",
            title="Dark Crime Movie 2",
            overview="A complete synopsis with enough evidence for semantic tagging.",
            popularity=90.0,
            vote_average=7.5,
            vote_count=1300,
        ),
    ]
    db.add_all(movies)
    db.commit()

    # Create movie tags
    movie_tags = [
        MovieTag(tmdb_id=1, tag_id=dark_tag.id, confidence=0.9, media_type="movie"),
        MovieTag(tmdb_id=1, tag_id=crime_tag.id, confidence=0.85, media_type="movie"),
        MovieTag(tmdb_id=2, tag_id=dark_tag.id, confidence=0.8, media_type="movie"),
        MovieTag(tmdb_id=2, tag_id=crime_tag.id, confidence=0.9, media_type="movie"),
    ]
    db.add_all(movie_tags)
    db.commit()

    # Create category
    category = UniversalCategory(
        id="dark-crime",
        name="Dark Crime Dramas",
        tier=1,
        sort_order=1,
        media_type="movie",
        tag_formula={"required": ["Dark", "Crime"], "min_required": 2},
    )
    db.add(category)
    db.commit()

    # Generate catalog
    seed_relevance(db)
    generator = CatalogGenerator(db)
    ranked = generator.generate_universal_catalog(category, limit=10)
    tmdb_ids = [tmdb_id for tmdb_id, _ in ranked]

    assert len(tmdb_ids) == 2
    assert 1 in tmdb_ids
    assert 2 in tmdb_ids
    # Scores are real now, not the hardcoded 0.0 this module wrote for years.
    assert all(score > 0 for _, score in ranked)


# =============================================================================
# API Tests
# =============================================================================


def test_root_endpoint(client):
    """Test root endpoint returns landing page HTML."""
    response = client.get("/")
    assert response.status_code == 200
    assert "Curatio" in response.text


def test_health_check(client):
    """Test health check endpoint."""
    response = client.get("/health")
    assert response.status_code == 200
    assert "status" in response.json()


# =============================================================================
# Integration Tests
# =============================================================================


@pytest.mark.integration
def test_full_tagging_to_catalog_flow(db):
    """Integration test: Tag movies → Store in DB → Generate catalog."""
    # Step 1: Create tags
    tags = [
        Tag(name="Dark", category="mood"),
        Tag(name="Gritty", category="mood"),
        Tag(name="Crime", category="genre"),
    ]
    db.add_all(tags)
    db.commit()

    # Step 2: Create movie metadata
    movie = MediaMetadata(
        tmdb_id=999,
        media_type="movie",
        title="Test Dark Crime Movie",
        overview="A dark and gritty crime story with enough evidence for tagging.",
        popularity=100.0,
        vote_average=8.5,
        vote_count=1500,
    )
    db.add(movie)
    db.commit()

    # Step 3: Add tags to movie
    movie_tags = [
        MovieTag(tmdb_id=999, tag_id=tags[0].id, confidence=0.95, media_type="movie"),
        MovieTag(tmdb_id=999, tag_id=tags[1].id, confidence=0.90, media_type="movie"),
        MovieTag(tmdb_id=999, tag_id=tags[2].id, confidence=0.88, media_type="movie"),
    ]
    db.add_all(movie_tags)
    db.commit()

    # Step 4: Create category
    category = UniversalCategory(
        id="integration-test",
        name="Integration Test Category",
        tier=1,
        sort_order=1,
        media_type="movie",
        tag_formula={"required": ["Dark", "Crime"], "min_required": 2},
    )
    db.add(category)
    db.commit()

    # Step 5: Generate catalog
    seed_relevance(db)
    generator = CatalogGenerator(db)
    tmdb_ids = generator.generate_universal_catalog_ids(category)

    # Step 6: Verify
    assert 999 in tmdb_ids


# =============================================================================
# Performance Tests
# =============================================================================


@pytest.mark.performance
def test_catalog_query_performance(db):
    """Test that catalog queries are fast (< 100ms)."""
    import time

    # Create test data
    tags = [Tag(name=f"Tag{i}", category="test") for i in range(10)]
    db.add_all(tags)
    db.commit()

    # Create 1000 test movies
    for i in range(1000):
        movie = MediaMetadata(
            tmdb_id=i,
            media_type="movie",
            title=f"Movie {i}",
            overview="A complete synopsis with enough evidence for semantic tagging.",
            popularity=float(i),
            # Above the catalog quality floor, so this measures the query
            # rather than an empty result set. vote_average matters as much as
            # the count: the Bayesian fallback reads a NULL average as 0.
            vote_average=7.4,
            vote_count=1000,
        )
        db.add(movie)

        # Add random tags
        for j in range(5):
            movie_tag = MovieTag(
                tmdb_id=i, tag_id=tags[j % 10].id, confidence=0.8, media_type="movie"
            )
            db.add(movie_tag)

    db.commit()

    # Create category
    category = UniversalCategory(
        id="perf-test",
        name="Performance Test",
        tier=1,
        sort_order=1,
        media_type="movie",
        tag_formula={"required": ["Tag0", "Tag1"], "min_required": 2},
    )
    db.add(category)
    db.commit()

    # Measure query time
    seed_relevance(db)
    generator = CatalogGenerator(db)

    start = time.time()
    results = generator.generate_universal_catalog(category, limit=100)
    duration = time.time() - start

    assert duration < 0.1  # Should be under 100ms
    assert len(results) > 0


# =============================================================================
# Error Handling Tests
# =============================================================================


def test_invalid_tag_formula(db):
    """Test handling of invalid tag formula."""
    category = UniversalCategory(
        id="invalid-test",
        name="Invalid Test",
        tier=1,
        sort_order=1,
        media_type="movie",
        tag_formula={"required": ["NonexistentTag"], "min_required": 1},
    )
    db.add(category)
    db.commit()

    generator = CatalogGenerator(db)

    # Should not crash, just return empty results
    results = generator.generate_universal_catalog(category)
    assert isinstance(results, list)


@pytest.mark.asyncio
async def test_gemini_api_error_handling():
    """Test handling of Gemini API errors."""
    tagging_engine = GeminiTaggingEngine()

    # Empty input must not touch the network.
    results = await tagging_engine.tag_items([])
    assert results == []


# =============================================================================
# Security Tests
# =============================================================================


# =============================================================================
# Scheduler Tests
# =============================================================================


def test_parse_time_valid():
    """Test parsing valid HH:MM time strings."""
    from app.scheduler import parse_time

    assert parse_time("03:00") == (3, 0)
    assert parse_time("00:00") == (0, 0)
    assert parse_time("23:59") == (23, 59)
    assert parse_time("12:30") == (12, 30)


def test_parse_time_invalid():
    """Test parsing invalid time strings raises ValueError."""
    from app.scheduler import parse_time

    with pytest.raises(ValueError):
        parse_time("25:00")
    with pytest.raises(ValueError):
        parse_time("12:60")
    with pytest.raises(ValueError):
        parse_time("invalid")
    with pytest.raises(ValueError):
        parse_time("12")


def test_seconds_until():
    """Test seconds_until returns a positive value."""
    from app.scheduler import seconds_until

    result = seconds_until(3, 0)
    assert result > 0
    # Should be at most ~24 hours
    assert result <= 86400


# =============================================================================
# Daily Update Tests
# =============================================================================


def _tag_title(db, tmdb_id, media_type="movie", tag_name="_filter_probe"):
    """Attach one tag to a title so filter_new_items considers it done."""
    tag = db.query(Tag).filter(Tag.name == tag_name).first()
    if tag is None:
        tag = Tag(name=tag_name, category="mood")
        db.add(tag)
        db.flush()
    db.add(
        MovieTag(tmdb_id=tmdb_id, media_type=media_type, tag_id=tag.id, confidence=0.9)
    )
    db.commit()


def test_filter_new_items(db):
    """filter_new_items removes already-TAGGED items."""
    from workers.daily_update import filter_new_items

    db.add(
        MediaMetadata(
            tmdb_id=100, media_type="movie", title="Tagged Movie", popularity=50.0
        )
    )
    db.commit()
    _tag_title(db, 100)

    items = [
        {"id": 100, "title": "Tagged Movie"},
        {"id": 200, "title": "New Movie"},
    ]

    result = filter_new_items(db, items, "movie")

    assert len(result) == 1
    assert result[0]["id"] == 200


def test_filter_new_items_keys_off_tags_not_metadata(db):
    """Regression for the self-perpetuating untagged backlog.

    personalized_sync inserts MediaMetadata rows for trending/popular/imported
    titles without tagging them. This filter used to test media_metadata, so
    those rows were treated as "already exists" and were never tagged by any
    daily run -- only a full initial_build could reach them. In production that
    backlog reached ~150k rows, 42% of the library.
    """
    from workers.daily_update import filter_new_items

    db.add(
        MediaMetadata(
            tmdb_id=700,
            media_type="movie",
            title="Backfilled But Untagged",
            popularity=50.0,
        )
    )
    db.commit()

    result = filter_new_items(
        db, [{"id": 700, "title": "Backfilled But Untagged"}], "movie"
    )

    assert [i["id"] for i in result] == [
        700
    ], "a title with metadata but no tags must still be offered for tagging"


def test_filter_new_items_skips_a_current_terminal_attempt(db):
    """Nightly acquisition must not repay for a known sparse title forever."""
    from workers.daily_update import filter_new_items
    from workers.tagging_common import tagger_version

    row = MediaMetadata(
        tmdb_id=701,
        media_type="movie",
        title="Terminally Sparse",
        popularity=50.0,
        tag_attempt_version=tagger_version(),
        tag_attempt_status="too_few_valid_tags",
    )
    db.add(row)
    db.commit()

    assert filter_new_items(db, [{"id": 701}], "movie") == []


def test_filter_new_items_is_media_type_scoped(db):
    """A tagged movie must not suppress the same tmdb_id as a TV title."""
    from workers.daily_update import filter_new_items

    _tag_title(db, 800, media_type="movie")

    assert [i["id"] for i in filter_new_items(db, [{"id": 800}], "movie")] == []
    assert [i["id"] for i in filter_new_items(db, [{"id": 800}], "tv")] == [800]


def test_filter_new_items_all_new(db):
    """Test filter_new_items when all items are new."""
    from workers.daily_update import filter_new_items

    items = [
        {"id": 300, "title": "New Movie 1"},
        {"id": 400, "title": "New Movie 2"},
    ]

    result = filter_new_items(db, items, "movie")
    assert len(result) == 2


def test_filter_new_items_all_existing(db):
    """filter_new_items returns nothing when every item is already tagged."""
    from workers.daily_update import filter_new_items

    for tmdb_id in [500, 600]:
        db.add(
            MediaMetadata(
                tmdb_id=tmdb_id,
                media_type="movie",
                title=f"Movie {tmdb_id}",
                popularity=50.0,
            )
        )
    db.commit()
    for tmdb_id in [500, 600]:
        _tag_title(db, tmdb_id)

    items = [
        {"id": 500, "title": "Movie 500"},
        {"id": 600, "title": "Movie 600"},
    ]

    result = filter_new_items(db, items, "movie")
    assert len(result) == 0


# =============================================================================
# Category Completeness Tests
# =============================================================================


def test_universal_categories_count(db):
    """Test that all 40 universal categories are created."""
    from app.categories import seed_categories
    from app.models import UniversalCategory

    seed_categories(db)

    count = db.query(UniversalCategory).count()
    assert count == 40, f"Expected 40 categories, got {count}"


def test_universal_categories_no_duplicates(db):
    """Test that no duplicate category IDs exist."""
    from app.categories import seed_categories
    from app.models import UniversalCategory

    seed_categories(db)

    categories = db.query(UniversalCategory).all()
    ids = [c.id for c in categories]
    assert len(ids) == len(set(ids)), "Duplicate category IDs found"


def test_universal_categories_have_valid_formulas(db):
    """Test that all categories have valid tag formulas."""
    from app.categories import seed_categories
    from app.models import UniversalCategory

    seed_categories(db)

    categories = db.query(UniversalCategory).all()
    for cat in categories:
        formula = cat.tag_formula
        assert "required" in formula, f"{cat.id} missing 'required' in formula"
        assert "min_required" in formula, f"{cat.id} missing 'min_required'"
        # Era categories use mandatory + required; total constraint tags must be >= 2
        mandatory = formula.get("mandatory", [])
        required = formula["required"]
        total_tags = len(mandatory) + len(required)
        assert (
            total_tags >= 2
        ), f"{cat.id} needs >= 2 total constraint tags (has {total_tags})"
        assert formula["min_required"] >= 1, f"{cat.id} min_required should be >= 1"


def test_seed_categories_idempotent(db):
    """Test that seeding categories twice doesn't create duplicates."""
    from app.categories import seed_categories
    from app.models import UniversalCategory

    seed_categories(db)
    seed_categories(db)

    count = db.query(UniversalCategory).count()
    assert count == 40, f"Expected 40 after double seed, got {count}"


# =============================================================================
# Run Tests
# =============================================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])

# =============================================================================
# Chromaprint Intro Skipper-style Matcher Tests
# =============================================================================


def test_chromaprint_timing_sanity_and_roundtrip():
    from workers.fingerprint_worker import (
        CHROMAPRINT_HASH_WINDOW_DURATION,
        CHROMAPRINT_SAMPLE_DURATION,
        point_to_seconds,
        seconds_to_point,
    )

    inferred = 4825 * CHROMAPRINT_SAMPLE_DURATION + CHROMAPRINT_HASH_WINDOW_DURATION
    assert inferred == pytest.approx(600, abs=1.0)
    for seconds in [0, 30, 165, 599.5]:
        assert point_to_seconds(seconds_to_point(seconds)) == pytest.approx(
            seconds, abs=CHROMAPRINT_SAMPLE_DURATION / 2
        )


def _make_points(length, seed=1000):
    value = seed & 0xFFFFFFFF
    points = []
    for _ in range(length):
        value = (1664525 * value + 1013904223) & 0xFFFFFFFF
        points.append(value)
    return points


def test_shift_discovery_positive_and_negative():
    from workers.fingerprint_worker import (
        CHROMAPRINT_SAMPLE_DURATION,
        _discover_candidate_shifts,
        _find_chromaprint_ranges,
        point_to_seconds,
    )

    shared = _make_points(260, seed=900000)
    fp_a = _make_points(500, seed=100)
    fp_b = _make_points(560, seed=2000000)
    fp_a[100 : 100 + len(shared)] = shared
    fp_b[150 : 150 + len(shared)] = shared

    assert 50 in _discover_candidate_shifts(fp_a, fp_b)
    ranges = _find_chromaprint_ranges(fp_a, fp_b, "test-pos", 20, 120)
    assert ranges
    assert ranges[0]["shift"] == 50
    assert ranges[0]["start_sec_a"] == pytest.approx(point_to_seconds(100), abs=0.2)
    assert ranges[0]["duration_sec_a"] == pytest.approx(
        len(shared) * CHROMAPRINT_SAMPLE_DURATION, abs=0.2
    )

    fp_a2 = _make_points(560, seed=300)
    fp_b2 = _make_points(500, seed=3000000)
    fp_a2[150 : 150 + len(shared)] = shared
    fp_b2[100 : 100 + len(shared)] = shared
    assert -50 in _discover_candidate_shifts(fp_a2, fp_b2)
    ranges = _find_chromaprint_ranges(fp_a2, fp_b2, "test-neg", 20, 120)
    assert ranges[0]["shift"] == -50


def test_contiguous_gaps_are_tolerated_or_split():
    from workers.fingerprint_worker import _find_chromaprint_ranges, seconds_to_point

    shared = _make_points(seconds_to_point(30), seed=700000)
    small_gap_points = seconds_to_point(2.0)
    large_gap_points = seconds_to_point(5.0)

    fp_a = _make_points(900, seed=1)
    fp_b = _make_points(900, seed=2)
    fp_a[100 : 100 + len(shared)] = shared
    fp_b[100 : 100 + len(shared)] = shared
    fp_a[
        100
        + len(shared)
        + small_gap_points : 100
        + len(shared)
        + small_gap_points
        + len(shared)
    ] = shared
    fp_b[
        100
        + len(shared)
        + small_gap_points : 100
        + len(shared)
        + small_gap_points
        + len(shared)
    ] = shared
    ranges = _find_chromaprint_ranges(fp_a, fp_b, "test-small-gap", 20, 120)
    assert ranges[0]["duration_sec_a"] > 60

    fp_a = _make_points(1000, seed=3)
    fp_b = _make_points(1000, seed=4)
    fp_a[100 : 100 + len(shared)] = shared
    fp_b[100 : 100 + len(shared)] = shared
    fp_a[
        100
        + len(shared)
        + large_gap_points : 100
        + len(shared)
        + large_gap_points
        + len(shared)
    ] = shared
    fp_b[
        100
        + len(shared)
        + large_gap_points : 100
        + len(shared)
        + large_gap_points
        + len(shared)
    ] = shared
    ranges = _find_chromaprint_ranges(fp_a, fp_b, "test-large-gap", 20, 120)
    assert ranges[0]["duration_sec_a"] < 40


def test_credits_offset_and_reliability_support_math():
    from workers.fingerprint_worker import (
        _candidate_confidence,
        _choose_credits_candidate,
        _choose_intro_candidate,
    )

    tail_base = 2000
    candidate = {
        "episode": 1,
        "segment_type": "credits",
        "start_sec": tail_base + 70,
        "end_sec": tail_base + 100,
        "duration_sec": 30,
        "pair_key": "1x2",
        "avg_hamming_distance": 0,
        "matched_point_count": 200,
    }
    # Credits require two independent pair supports and terminal audio.
    selected, reason, diag = _choose_credits_candidate(
        [candidate, {**candidate, "pair_key": "1x3"}],
        [(1, 2), (1, 3)],
        1,
        2100,
    )
    assert reason is None
    assert selected["start_sec"] == 2070
    assert selected["end_sec"] == 2100

    # Confidence saturates support at SUPPORT_SATURATION_PAIRS confirming pairs: two
    # pristine, duration-consistent pairs are full support regardless of how many pairs the
    # season could form, so adding episodes never demotes a clean match. `possible` is still
    # reported for diagnostics.
    support, possible, confidence = _candidate_confidence(
        [
            {"pair_key": "1x3", "avg_hamming_distance": 0, "duration_sec": 30},
            {"pair_key": "3x4", "avg_hamming_distance": 0, "duration_sec": 30},
        ],
        [(1, 2), (1, 3), (1, 4), (2, 3), (2, 4), (3, 4)],
        3,
    )
    assert (support, possible) == (2, 3)
    assert confidence == pytest.approx(1.0)
    assert confidence <= 1.0

    # A single near-pristine pair now clears the serve gate via the strong-evidence floor,
    # but stays below the 0.85 cross-season reference bar so one pair never seeds a reference.
    one_support, _one_possible, one_confidence = _candidate_confidence(
        [{"pair_key": "1x2", "avg_hamming_distance": 0, "duration_sec": 30}],
        [(1, 2), (1, 3), (1, 4), (2, 3), (2, 4), (3, 4)],
        1,
    )
    assert one_support == 1
    assert one_confidence >= 0.70
    assert one_confidence < 0.85

    early_short = [
        {"start_sec": 30, "end_sec": 56, "duration_sec": 26, "pair_key": "1x2"}
    ]
    selected, reason, _ = _choose_intro_candidate(
        early_short, [(1, 2), (1, 3), (1, 4)], 1
    )
    assert selected is None
    assert reason == "no_reliable_intro_candidate"

    early_credits = [
        {**candidate, "start_sec": 1000, "end_sec": 1030, "duration_sec": 30}
    ]
    selected, reason, _ = _choose_credits_candidate(
        early_credits, [(1, 2), (1, 3)], 1, 2500
    )
    assert selected is None
    assert reason == "no_reliable_credits_candidate"


def test_short_credits_are_rejected_even_with_multiple_pairs():
    from workers.fingerprint_worker import _choose_credits_candidate

    base_candidate = {
        "episode": 5,
        "segment_type": "credits",
        "start_sec": 2988,
        "end_sec": 3000,
        "duration_sec": 12,
        "avg_hamming_distance": 1.5,
        "matched_point_count": 100,
    }

    selected, reason, diag = _choose_credits_candidate(
        [{**base_candidate, "pair_key": "3x5"}],
        [(3, 5), (5, 7)],
        5,
        3000,
    )
    assert selected is None
    assert reason == "no_reliable_credits_candidate"
    assert diag["candidate_cluster_count"] == 1

    selected, reason, diag = _choose_credits_candidate(
        [
            {**base_candidate, "pair_key": "3x5"},
            {**base_candidate, "pair_key": "5x7"},
        ],
        [(3, 5), (5, 7)],
        5,
        3000,
    )
    assert selected is None
    assert reason == "no_reliable_credits_candidate"
    assert diag["clusters"][0]["support"] == 2


def test_short_terminal_credits_rejects_early_up_next_cue():
    from workers.fingerprint_worker import _choose_credits_candidate

    candidate = {
        "episode": 1,
        "segment_type": "credits",
        "start_sec": 1800,
        "end_sec": 1817,
        "duration_sec": 17,
        "pair_key": "1x2",
        "avg_hamming_distance": 0.5,
        "matched_point_count": 140,
    }

    selected, reason, diag = _choose_credits_candidate(
        [candidate],
        [(1, 2)],
        1,
        2000,
    )

    assert selected is None
    assert reason == "no_reliable_credits_candidate"
    assert diag["rejection_reason"] == "credits_require_two_pair_terminal_audio_cluster"


def test_long_credits_rejected_when_too_early():
    # A long (>=60s), high-confidence fingerprint match sitting mid-episode — a recurring
    # outro song / recap bed shared across neighbours — used to be accepted as credits via
    # the `duration >= 60 and confidence >= 0.90` bypass of the 0.85 start-ratio gate, with
    # no absolute lower bound. It must now be rejected below CREDITS_MIN_START_RATIO.
    from workers.fingerprint_worker import _choose_credits_candidate

    base = {
        "episode": 1,
        "segment_type": "credits",
        "start_sec": 1500,  # 1500 / 2500 = 0.60
        "end_sec": 1590,
        "duration_sec": 90,
        "avg_hamming_distance": 0,
        "matched_point_count": 700,
    }
    candidates = [
        {**base, "pair_key": "1x2"},
        {**base, "pair_key": "1x3"},
    ]
    selected, reason, diag = _choose_credits_candidate(
        candidates, [(1, 2), (1, 3)], 1, 2500
    )
    assert selected is None
    assert reason == "no_reliable_credits_candidate"
    assert diag["clusters"][0]["support"] == 2
    assert diag["rejection_reason"] == "credits_require_two_pair_terminal_audio_cluster"


def test_long_non_terminal_credits_have_no_confidence_escape():
    from workers.fingerprint_worker import _choose_credits_candidate

    base = {
        "episode": 1,
        "segment_type": "credits",
        "start_sec": 980,  # 980 / 1200 = 0.817, inside [0.75, 0.85)
        "end_sec": 1070,
        "duration_sec": 90,
        "avg_hamming_distance": 0,
        "matched_point_count": 700,
    }
    candidates = [
        {**base, "pair_key": "1x2"},
        {**base, "pair_key": "1x3"},
    ]
    selected, reason, diag = _choose_credits_candidate(
        candidates, [(1, 2), (1, 3)], 1, 1200
    )
    assert selected is None
    assert reason == "no_reliable_credits_candidate"
    assert diag["rejection_reason"] == "credits_require_two_pair_terminal_audio_cluster"


def test_credits_rejected_when_too_far_from_end():
    # The regression test for the reported bug. A 60-min episode: a long, confident
    # candidate at 3000s has ratio 0.833 and so clears CREDITS_MIN_START_RATIO and the
    # 0.85 soft gate's long/confident bypass — every ratio gate passes. It is still 10
    # minutes before the end, which the absolute backstop rejects. Ratio gates cannot
    # bind here at all: the 600s tail window makes ratio < 0.833 unreachable.
    from workers.fingerprint_worker import _choose_credits_candidate

    base = {
        "episode": 1,
        "segment_type": "credits",
        "start_sec": 3000,  # 3000 / 3600 = 0.833
        "end_sec": 3120,
        "duration_sec": 120,
        "avg_hamming_distance": 0,
        "matched_point_count": 900,
    }
    candidates = [
        {**base, "pair_key": "1x2"},
        {**base, "pair_key": "1x3"},
    ]
    selected, reason, diag = _choose_credits_candidate(
        candidates, [(1, 2), (1, 3)], 1, 3600
    )
    assert selected is None
    assert reason == "no_reliable_credits_candidate"
    assert diag["clusters"][0]["support"] == 2
    assert diag["rejection_reason"] == "credits_require_two_pair_terminal_audio_cluster"


def test_credits_accepted_just_inside_distance_cap():
    # Boundary companion: exactly at the cap is accepted (the check is a strict >).
    from workers.fingerprint_worker import (
        CREDITS_MAX_START_DISTANCE_FROM_END_SEC,
        _choose_credits_candidate,
    )

    start = 3600 - CREDITS_MAX_START_DISTANCE_FROM_END_SEC
    base = {
        "episode": 1,
        "segment_type": "credits",
        "start_sec": start,
        "end_sec": start + 120,
        "duration_sec": 120,
        "avg_hamming_distance": 0,
        "matched_point_count": 900,
    }
    candidates = [
        {**base, "pair_key": "1x2"},
        {**base, "pair_key": "1x3"},
    ]
    selected, reason, diag = _choose_credits_candidate(
        candidates, [(1, 2), (1, 3)], 1, 3600
    )
    assert reason is None
    assert selected["start_sec"] == start
    assert diag["credits_start_distance_from_end_sec"] == pytest.approx(
        float(CREDITS_MAX_START_DISTANCE_FROM_END_SEC)
    )


def test_intro_candidate_allows_three_minute_max_duration():
    from workers.fingerprint_worker import _choose_intro_candidate

    selected, reason, _ = _choose_intro_candidate(
        [
            {
                "episode": 1,
                "segment_type": "intro",
                "start_sec": 90,
                "end_sec": 270,
                "duration_sec": 180,
                "pair_key": "1x2",
                "avg_hamming_distance": 0.5,
                "matched_point_count": 1400,
            }
        ],
        [(1, 2)],
        1,
    )

    assert reason is None
    assert selected["duration_sec"] == 180

    selected, reason, _ = _choose_intro_candidate(
        [
            {
                "episode": 1,
                "segment_type": "intro",
                "start_sec": 90,
                "end_sec": 271,
                "duration_sec": 181,
                "pair_key": "1x2",
                "avg_hamming_distance": 0.5,
                "matched_point_count": 1400,
            }
        ],
        [(1, 2)],
        1,
    )

    assert selected is None
    assert reason == "no_reliable_intro_candidate"


def test_short_intro_fallback_accepts_strong_repeated_cold_open():
    from workers.fingerprint_worker import _choose_intro_candidate, seconds_to_point

    base_candidate = {
        "episode": 1,
        "segment_type": "intro",
        "start_sec": 0.0,
        "end_sec": 13.5,
        "duration_sec": 13.5,
        "avg_hamming_distance": 0.25,
        "matched_point_count": seconds_to_point(13.5),
    }

    selected, reason, diag = _choose_intro_candidate(
        [
            {**base_candidate, "pair_key": "1x2"},
            {**base_candidate, "pair_key": "1x3", "end_sec": 13.4},
        ],
        [(1, 2), (1, 3)],
        1,
    )

    assert reason is None
    assert selected["short_intro_fallback"] is True
    assert selected["duration_sec"] == 13.5
    assert diag["candidate_cluster_count"] == 1
    assert diag["support"] == 2


def test_short_intro_fallback_rejects_single_or_noisy_matches():
    from workers.fingerprint_worker import _choose_intro_candidate, seconds_to_point

    candidate = {
        "episode": 1,
        "segment_type": "intro",
        "start_sec": 0.0,
        "end_sec": 13.5,
        "duration_sec": 13.5,
        "pair_key": "1x2",
        "avg_hamming_distance": 0.25,
        "matched_point_count": seconds_to_point(13.5),
    }

    selected, reason, diag = _choose_intro_candidate(
        [candidate],
        [(1, 2), (1, 3)],
        1,
    )

    assert selected is None
    assert reason == "no_reliable_intro_candidate"
    assert diag["candidate_cluster_count"] == 1

    selected, reason, _ = _choose_intro_candidate(
        [
            {**candidate, "pair_key": "1x2", "avg_hamming_distance": 1.5},
            {**candidate, "pair_key": "1x3", "avg_hamming_distance": 1.5},
        ],
        [(1, 2), (1, 3)],
        1,
    )

    assert selected is None
    assert reason == "no_reliable_intro_candidate"


def test_source_hashes_do_not_influence_marker_selection():
    from workers.fingerprint_worker import _choose_intro_candidate

    selected, reason, diag = _choose_intro_candidate(
        [
            {
                "episode": 1,
                "segment_type": "intro",
                "start_sec": 120,
                "end_sec": 180,
                "duration_sec": 60,
                "pair_key": "1x2",
                "avg_hamming_distance": 0.1,
                "matched_point_count": 500,
                "source_mismatch": True,
            },
            {
                "episode": 1,
                "segment_type": "intro",
                "start_sec": 121,
                "end_sec": 170,
                "duration_sec": 49,
                "pair_key": "1x3",
                "avg_hamming_distance": 0.2,
                "matched_point_count": 420,
                "source_mismatch": False,
            },
        ],
        [(1, 2), (1, 3)],
        1,
    )

    assert reason is None
    assert selected["start_sec"] == 120
    assert "source_mismatch_penalty_applied" not in diag


def test_media_url_sanitizer_removes_query_tokens_and_keeps_release_suffix():
    from workers.fingerprint_worker import _sanitize_media_url_for_log

    sanitized = _sanitize_media_url_for_log(
        "https://example.test/v0/proxy/signed-token/FROM%202022%20S02.mkv?token=secret"
    )

    assert sanitized == "https://example.test/…/FROM%202022%20S02.mkv"
    assert "secret" not in sanitized


def test_credits_ranges_prefer_terminal_match_over_longer_mid_tail_match():
    from workers.fingerprint_worker import (
        _find_chromaprint_ranges,
        seconds_to_point,
    )

    fp_a = _make_points(seconds_to_point(600), seed=1100)
    fp_b = _make_points(seconds_to_point(600), seed=2200)

    mid_shared = _make_points(seconds_to_point(36), seed=3300)
    terminal_shared = _make_points(seconds_to_point(26), seed=4400)

    mid_start = seconds_to_point(415)
    terminal_start = seconds_to_point(570)
    fp_a[mid_start : mid_start + len(mid_shared)] = mid_shared
    fp_b[mid_start : mid_start + len(mid_shared)] = mid_shared
    fp_a[terminal_start : terminal_start + len(terminal_shared)] = terminal_shared
    fp_b[terminal_start : terminal_start + len(terminal_shared)] = terminal_shared

    ranges = _find_chromaprint_ranges(
        fp_a,
        fp_b,
        "test-credits-terminal",
        15,
        180,
        segment_type="credits",
        pair_key="1x2",
    )

    assert ranges
    assert ranges[0]["start_sec_a"] == pytest.approx(570, abs=0.25)
    assert ranges[0]["credits_tail_end_distance_sec"] < 30


def test_intro_keeps_alternate_ranges_from_same_pair_for_selection():
    from workers.fingerprint_worker import _choose_intro_candidate

    candidates = [
        {
            "episode": 1,
            "segment_type": "intro",
            "start_sec": 10,
            "end_sec": 42,
            "duration_sec": 32,
            "pair_key": "1x2",
            "avg_hamming_distance": 0.5,
            "matched_point_count": 260,
        },
        {
            "episode": 1,
            "segment_type": "intro",
            "start_sec": 95,
            "end_sec": 121,
            "duration_sec": 26,
            "pair_key": "1x2",
            "avg_hamming_distance": 0.5,
            "matched_point_count": 210,
        },
    ]

    selected, reason, diag = _choose_intro_candidate(candidates, [(1, 2)], 1)

    assert reason is None
    assert selected["start_sec"] == 10
    assert diag["support"] == 1


def test_waveform_fallback_disabled_and_invalid_feature_units(monkeypatch):
    import subprocess
    from workers import fingerprint_worker as fw

    assert fw.ENABLE_WAVEFORM_FALLBACK is False
    monkeypatch.setattr(
        subprocess,
        "check_output",
        lambda *args, **kwargs: "\n".join(["lavfi.astats.Overall.RMS_level=-6"] * 1000),
    )
    assert fw._extract_audio_features("dummy", 0, 10) == []


def test_stale_cleanup_preserves_manual_and_client_feedback(db):
    from workers.fingerprint_worker import _delete_auto_markers

    db.add_all(
        [
            SegmentMarker(
                imdb_id="tttest",
                season=1,
                episode=1,
                segment_type="intro",
                start_sec=1,
                end_sec=10,
                confidence=0.5,
                source="chromaprint",
            ),
            SegmentMarker(
                imdb_id="tttest",
                season=1,
                episode=1,
                segment_type="intro",
                start_sec=2,
                end_sec=11,
                confidence=1.0,
                source="manual",
                is_user_provided=True,
            ),
            SegmentMarker(
                imdb_id="tttest",
                season=1,
                episode=1,
                segment_type="intro",
                start_sec=3,
                end_sec=12,
                confidence=1.0,
                source="client_feedback",
                is_user_provided=True,
            ),
        ]
    )
    db.commit()

    assert _delete_auto_markers(db, "tttest", 1, 1, "intro") == 1
    remaining_sources = {m.source for m in db.query(SegmentMarker).all()}
    assert remaining_sources == {"manual", "client_feedback"}


def test_waveform_fallback_marker_save_blocked(db):
    from workers.fingerprint_worker import _save_auto_marker

    deleted = _save_auto_marker(
        db,
        "tttest",
        1,
        1,
        "intro",
        {
            "start_sec": 1,
            "end_sec": 50,
            "source": "waveform_fallback",
        },
        0.5,
    )
    db.commit()
    assert deleted == 0
    assert db.query(SegmentMarker).count() == 0


def test_full_season_pair_plan_includes_late_unresolved_neighbors():
    from workers.fingerprint_worker import build_full_season_pair_plan

    plan = build_full_season_pair_plan(list(range(1, 11)), {7, 8, 9, 10})

    assert (1, 2) in plan
    assert (1, 3) in plan
    assert (7, 8) in plan
    assert (7, 9) in plan
    assert (8, 9) in plan
    assert (8, 10) in plan
    assert (9, 10) in plan
    assert plan.index((1, 2)) < plan.index((1, 3))


def test_stale_cleanup_skipped_for_pending_companion():
    from workers.fingerprint_worker import (
        classify_episode_marker_state,
        should_cleanup_stale_marker,
    )

    state, reason = classify_episode_marker_state(
        None, "no_reliable_intro_candidate", valid_comparisons=1
    )

    assert state == "pending_companion"
    assert reason == "no_reliable_intro_candidate"
    assert should_cleanup_stale_marker(state) is False
    assert should_cleanup_stale_marker("rejected") is True
    assert should_cleanup_stale_marker("rejected", source="manual") is False
    assert should_cleanup_stale_marker("rejected", source="client_feedback") is False


def test_credits_duration_tolerance_accepts_just_under_minimum():
    from workers.fingerprint_worker import _choose_credits_candidate

    selected, reason, diag = _choose_credits_candidate(
        [
            {
                "episode": 1,
                "segment_type": "credits",
                "start_sec": 2285.02,
                "end_sec": 2300,
                "duration_sec": 14.98,
                "pair_key": "1x2",
                "avg_hamming_distance": 0.5,
                "matched_point_count": 130,
            },
            {
                "episode": 1,
                "segment_type": "credits",
                "start_sec": 2285.02,
                "end_sec": 2300,
                "duration_sec": 14.98,
                "pair_key": "1x3",
                "avg_hamming_distance": 0.5,
                "matched_point_count": 130,
            },
        ],
        [(1, 2), (1, 3)],
        1,
        2300,
    )

    assert reason is None
    assert selected["duration_sec"] == pytest.approx(14.98)
    assert diag["credits_start_ratio"] == pytest.approx(0.993, abs=0.001)


def test_distinct_episode_source_urls_do_not_reduce_confidence():
    from workers.fingerprint_worker import _choose_intro_candidate, seconds_to_point

    selected, reason, diag = _choose_intro_candidate(
        [
            {
                "episode": 1,
                "segment_type": "intro",
                "start_sec": 100,
                "end_sec": 145,
                "duration_sec": 45,
                "pair_key": "1x2",
                "avg_hamming_distance": 0.5,
                "matched_point_count": seconds_to_point(45),
                "source_mismatch": True,
            }
        ],
        [(1, 2)],
        1,
    )

    assert reason is None
    assert selected["confidence"] < 1.0
    assert "source_mismatch_pairs" not in diag


def test_finalization_classifies_each_episode_before_cleanup_decision():
    from workers.fingerprint_worker import (
        classify_episode_marker_state,
        should_cleanup_stale_marker,
    )

    states = {
        1: classify_episode_marker_state({"confidence": 0.95}, None, 2)[0],
        2: classify_episode_marker_state(None, "no_reliable_intro_candidate", 2)[0],
        3: classify_episode_marker_state(None, "no_reliable_intro_candidate", 1)[0],
    }

    assert states == {1: "confirmed", 2: "rejected", 3: "pending_companion"}
    assert should_cleanup_stale_marker(states[1]) is False
    assert should_cleanup_stale_marker(states[2]) is True
    assert should_cleanup_stale_marker(states[3]) is False


# =============================================================================
# New-profile catalog provisioning Tests
#
# Regression coverage for the bug where new profiles permanently lose their
# Trending/New Releases/Discover rows: the foreground sync stamped last_sync
# even when the TMDB fetch came back empty, so the profile stopped re-triggering
# the sync.
# =============================================================================


def _make_profile(db, user_key="provision-test-key"):
    """Create a minimal viewing profile for catalog tests."""
    from app.models import User

    user = User(user_key=user_key, auth_source="local")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _full_common_data():
    """Common TMDB data with every slot populated (a healthy fetch)."""
    return {
        "trending-movie": [101, 102],
        "trending-series": [201, 202],
        "new-releases-movie": [103, 104],
        "new-releases-series": [203, 204],
        "popular-movie": [105, 106],
        "popular-series": [205, 206],
    }


def _seed_metadata(db, common_data):
    """Insert MediaMetadata so the backfill step finds everything locally."""
    seen = set()
    for slot, ids in common_data.items():
        media_type = "tv" if "series" in slot else "movie"
        for tmdb_id in ids:
            if (tmdb_id, media_type) in seen:
                continue
            seen.add((tmdb_id, media_type))
            db.add(
                MediaMetadata(
                    tmdb_id=tmdb_id,
                    media_type=media_type,
                    title=f"Title {tmdb_id}",
                    popularity=1.0,
                    vote_count=10,
                )
            )
    db.commit()


@pytest.mark.asyncio
async def test_common_catalogs_set_last_sync_when_core_rows_present(db):
    """Healthy TMDB fetch → core rows created and the profile is marked synced."""
    from workers.personalized_sync import _generate_common_catalogs
    from app.models import UserCatalog

    user = _make_profile(db)
    common_data = _full_common_data()
    _seed_metadata(db, common_data)

    catalog_gen = CatalogGenerator(db)
    await _generate_common_catalogs(
        user, db, catalog_gen, 0, "tester", common_data=common_data
    )

    slots = {
        c.slot_id for c in db.query(UserCatalog).filter(UserCatalog.user_id == user.id)
    }
    assert {
        "trending-movie",
        "trending-series",
        "new-releases-movie",
        "new-releases-series",
    } <= slots
    assert user.last_sync is not None


@pytest.mark.asyncio
async def test_common_catalogs_leave_last_sync_unset_when_core_rows_missing(db):
    """Failed TMDB fetch (empty slots) → no false rows and last_sync stays None.

    This is the core regression: without it the profile would be marked synced
    with zero common rows and would stop retrying them.
    """
    from workers.personalized_sync import _generate_common_catalogs
    from app.models import UserCatalog

    user = _make_profile(db)

    catalog_gen = CatalogGenerator(db)
    await _generate_common_catalogs(user, db, catalog_gen, 0, "tester", common_data={})

    rows = db.query(UserCatalog).filter(UserCatalog.user_id == user.id).count()
    assert rows == 0
    assert user.last_sync is None


@pytest.mark.asyncio
async def test_common_catalogs_recover_on_retry(db):
    """A failed sync followed by a healthy one populates rows and sets last_sync."""
    from workers.personalized_sync import _generate_common_catalogs
    from app.models import UserCatalog

    user = _make_profile(db)
    catalog_gen = CatalogGenerator(db)

    # First attempt: TMDB down.
    await _generate_common_catalogs(user, db, catalog_gen, 0, "tester", common_data={})
    assert user.last_sync is None

    # Second attempt: TMDB healthy.
    common_data = _full_common_data()
    _seed_metadata(db, common_data)
    await _generate_common_catalogs(
        user, db, catalog_gen, 0, "tester", common_data=common_data
    )
    assert user.last_sync is not None
    assert db.query(UserCatalog).filter(UserCatalog.user_id == user.id).count() >= 4


def test_common_data_cache_only_stores_full_results():
    """The shared common_data cache must never serve a partial/failed fetch."""
    import workers.personalized_sync as psync

    psync._common_data_cache = None
    try:
        # Partial result (a core slot empty) is not cached.
        partial = _full_common_data()
        partial["trending-movie"] = []
        psync._store_common_data(partial)
        assert psync._get_cached_common_data() is None

        # Fully-populated result is cached and returned.
        full = _full_common_data()
        psync._store_common_data(full)
        assert psync._get_cached_common_data() == full

        # An expired entry is treated as a miss.
        psync._common_data_cache = (
            psync.time.monotonic() - psync._COMMON_DATA_TTL - 1,
            full,
        )
        assert psync._get_cached_common_data() is None
    finally:
        psync._common_data_cache = None


def test_tmdb_is_retryable_retries_rate_limit_only():
    """429 (rate limit) is retryable; other 4xx are not; network errors are."""
    import httpx
    from app.tmdb_client import _is_retryable

    req = httpx.Request("GET", "https://api.themoviedb.org/3/trending/movie/day")

    rate_limited = httpx.HTTPStatusError(
        "429", request=req, response=httpx.Response(429, request=req)
    )
    not_found = httpx.HTTPStatusError(
        "404", request=req, response=httpx.Response(404, request=req)
    )

    assert _is_retryable(rate_limited) is True
    assert _is_retryable(not_found) is False
    assert _is_retryable(httpx.ConnectError("boom")) is True
    # Retrying a saturated pool re-queues behind the jam that caused the timeout.
    assert _is_retryable(httpx.PoolTimeout("")) is False


@pytest.mark.asyncio
async def test_tmdb_requests_never_exceed_the_admission_gate():
    """Concurrent callers queue on the gate rather than on the httpx pool."""
    import asyncio

    import httpx

    from app.tmdb_client import TMDBClient, _TMDB_MAX_INFLIGHT

    client = TMDBClient(api_key="test")
    in_flight = 0
    peak = 0

    class _FakeClient:
        async def get(self, url, params=None):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            try:
                await asyncio.sleep(0)
                return httpx.Response(
                    200,
                    json={"ok": True},
                    request=httpx.Request("GET", url),
                )
            finally:
                in_flight -= 1

    client._loop_client = lambda: _FakeClient()
    await asyncio.gather(
        *[client._request(f"/movie/{index}") for index in range(_TMDB_MAX_INFLIGHT * 3)]
    )

    assert peak <= _TMDB_MAX_INFLIGHT
