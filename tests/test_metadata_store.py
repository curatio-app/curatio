# ruff: noqa: E402
"""Writing metadata must not destroy what enrichment already learned.

``extract_metadata`` always returns the full column set, filling ``None`` for
anything the payload does not carry. Acquisition and the daily update work with
*list* payloads (``/discover``, ``/popular``, ``/top_rated``), which carry no
``external_ids``, no ``images``, no ``release_dates`` and no season counts.

``db.merge(MediaMetadata(**metadata_dict))`` writes every one of those attributes
including the ``None``s, so merging a list payload over an enriched row erased
``imdb_id``, ``logo_path``, ``digital_release_date`` and the season counts.
``imdb_id`` is load-bearing: MDBList matching, fingerprinting and stream
resolution all key off it, and only a detail call ever populates it.

That made re-running acquisition on the 354k-title production library actively
destructive, which is why D18 could not be attempted before this.
"""

import os
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_metadata_store.db")
os.environ.setdefault("TMDB_API_KEY", "test_key")
os.environ.setdefault("GEMINI_API_KEY", "test_key")
os.environ.setdefault("MASTER_PASSWORD", "test_password")
os.environ.setdefault("SECRET_KEY", "test_secret_key_at_least_32_chars_long")
os.environ.setdefault("BASE_URL", "http://localhost:8000")
os.environ.setdefault("SKIP_API_VALIDATION", "true")

from app.models import Base, MediaMetadata  # noqa: E402
from workers.metadata_store import upsert_many, upsert_media_metadata  # noqa: E402

TEST_DATABASE_URL = "sqlite:///./test_metadata_store.db"
engine = create_engine(TEST_DATABASE_URL, connect_args={"check_same_thread": False})
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture()
def db():
    Base.metadata.create_all(bind=engine)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


def _enriched_row(db, tmdb_id=603):
    """A row as it looks after the enricher has been over it."""
    row = MediaMetadata(
        tmdb_id=tmdb_id,
        media_type="movie",
        title="The Matrix",
        overview="A hacker learns the truth.",
        popularity=80.0,
        vote_average=8.2,
        vote_count=25000,
        release_date="1999-03-30",
        imdb_id="tt0133093",
        logo_path="/logo.png",
        digital_release_date="1999-09-21",
        number_of_seasons=None,
        raw_data={"credits": {"cast": [{"name": "Keanu Reeves"}]}, "big": "payload"},
    )
    db.add(row)
    db.commit()
    return row


def _list_payload(tmdb_id=603, **overrides):
    """What extract_metadata returns for a /discover-shaped item.

    Every detail-only column is present and None -- that is the whole problem.
    """
    payload = {
        "tmdb_id": tmdb_id,
        "media_type": "movie",
        "title": "The Matrix",
        "overview": "A hacker learns the truth.",
        "popularity": 95.0,
        "vote_average": 8.7,
        "vote_count": 26000,
        "release_date": "1999-03-30",
        "imdb_id": None,
        "logo_path": None,
        "digital_release_date": None,
        "next_episode_air_date": None,
        "number_of_seasons": None,
        "number_of_episodes": None,
        "raw_data": {"id": tmdb_id, "title": "The Matrix"},
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# The regression
# ---------------------------------------------------------------------------
def test_a_list_payload_does_not_erase_detail_columns(db):
    """The headline. Fails on the pre-fix `db.merge` path."""
    _enriched_row(db)

    upsert_media_metadata(db, _list_payload())
    db.commit()

    row = db.query(MediaMetadata).filter_by(tmdb_id=603).one()
    assert row.imdb_id == "tt0133093", "imdb_id drives MDBList and stream resolution"
    assert row.logo_path == "/logo.png"
    assert row.digital_release_date == "1999-09-21"
    assert row.raw_data.get("credits"), "the richer payload must not be downgraded"


def test_values_that_are_present_still_overwrite(db):
    """Non-destructive must not mean inert -- fresh data has to land."""
    _enriched_row(db)

    upsert_media_metadata(db, _list_payload())
    db.commit()

    row = db.query(MediaMetadata).filter_by(tmdb_id=603).one()
    assert row.popularity == 95.0
    assert row.vote_count == 26000


def test_list_payload_cannot_overwrite_detail_identity_or_release_date(db):
    _enriched_row(db)

    upsert_media_metadata(
        db,
        _list_payload(
            title="The Matrix Anniversary",
            original_title="Wrong title",
            release_date="2024-06-14",
        ),
    )
    db.commit()

    row = db.query(MediaMetadata).filter_by(tmdb_id=603).one()
    assert row.title == "The Matrix"
    assert row.original_title is None
    assert row.release_date == "1999-03-30"


def test_new_detail_payload_can_repair_canonical_release_date(db):
    _enriched_row(db)
    payload = _list_payload(
        release_date="1999-03-31",
        raw_data={"credits": {"cast": []}, "release_date": "1999-03-31"},
    )

    upsert_media_metadata(db, payload)
    db.commit()

    assert (
        db.query(MediaMetadata).filter_by(tmdb_id=603).one().release_date
        == "1999-03-31"
    )


def test_a_detail_payload_fills_in_what_was_missing(db):
    """The enricher's own path is unaffected: it has no Nones to skip."""
    db.add(MediaMetadata(tmdb_id=603, media_type="movie", title="The Matrix"))
    db.commit()

    upsert_media_metadata(db, _list_payload(imdb_id="tt0133093", logo_path="/l.png"))
    db.commit()

    row = db.query(MediaMetadata).filter_by(tmdb_id=603).one()
    assert row.imdb_id == "tt0133093"
    assert row.logo_path == "/l.png"


def test_a_new_title_is_inserted(db):
    inserted = upsert_media_metadata(db, _list_payload(tmdb_id=999))
    db.commit()

    assert inserted is True
    assert db.query(MediaMetadata).filter_by(tmdb_id=999).one().title == "The Matrix"


def test_the_same_id_in_the_other_media_type_is_a_different_title(db):
    """tmdb ids are only unique per type; keying on id alone would merge them."""
    _enriched_row(db, tmdb_id=1396)

    upsert_media_metadata(
        db, _list_payload(tmdb_id=1396, media_type="tv", title="Breaking Bad")
    )
    db.commit()

    assert db.query(MediaMetadata).filter_by(tmdb_id=1396).count() == 2
    assert db.query(MediaMetadata).filter_by(
        tmdb_id=1396, media_type="movie"
    ).one().title == ("The Matrix")


def test_raw_data_is_not_downgraded_from_detail_to_a_list_stub(db):
    """The subtle half of the bug.

    ``raw_data`` is never None in a list payload -- it is a *smaller dict* --
    so a plain non-null rule would happily replace a pruned detail document
    with an id-and-title stub. The row would keep ``enrich_version``, claiming
    to be enriched, while ``fingerprint_worker`` found no ``seasons`` in it.
    """
    _enriched_row(db)

    upsert_media_metadata(db, _list_payload())
    db.commit()

    raw = db.query(MediaMetadata).filter_by(tmdb_id=603).one().raw_data
    assert "credits" in raw


def test_a_richer_payload_does_replace_raw_data(db):
    """The guard is directional: enrichment must still be able to write."""
    db.add(
        MediaMetadata(
            tmdb_id=603, media_type="movie", title="The Matrix", raw_data={"id": 603}
        )
    )
    db.commit()

    upsert_media_metadata(
        db, _list_payload(raw_data={"id": 603, "credits": {"cast": []}})
    )
    db.commit()

    assert "credits" in db.query(MediaMetadata).filter_by(tmdb_id=603).one().raw_data


def test_a_payload_with_no_key_raises_rather_than_silently_skipping(db):
    """A shape change must be loud. Silence is how the original bug survived."""
    with pytest.raises(ValueError):
        upsert_media_metadata(db, {"title": "no ids"})


# ---------------------------------------------------------------------------
# Batching
# ---------------------------------------------------------------------------
def test_upsert_many_reports_inserts_and_updates_separately(db):
    _enriched_row(db)

    stats = upsert_many(
        db, [_list_payload(), _list_payload(tmdb_id=1), _list_payload(tmdb_id=2)]
    )

    assert stats == {"inserted": 2, "updated": 1}


def test_upsert_many_commits_in_batches_so_an_interrupted_run_keeps_its_work(db):
    """Same resumability property every other pipeline stage has."""
    rows = [_list_payload(tmdb_id=i) for i in range(1, 8)]

    with pytest.raises(ValueError):
        upsert_many(db, rows + [{"title": "boom"}], batch_size=5)

    # The first full batch was committed before the bad row was reached.
    db.rollback()
    assert db.query(MediaMetadata).count() == 5
