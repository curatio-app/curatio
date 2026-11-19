# ruff: noqa: E402
"""Implicit negative signal — abandoning a title is evidence.

Until now only ``action IN ('complete', 'imported')`` fed taste, so a title
someone bailed on after ten minutes was indistinguishable from one they never
started. A profile could therefore only ever accumulate evidence *for* things,
and a row the user repeatedly abandons had no mechanism for sinking.

The property under test is that a negative factor actually **subtracts**: it is
not enough for an abandoned title to be excluded, it has to pull its tags down
against the titles that were finished.
"""

import os
import sys
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_negative_signal.db")
os.environ.setdefault("TMDB_API_KEY", "test_key")
os.environ.setdefault("GEMINI_API_KEY", "test_key")
os.environ.setdefault("MASTER_PASSWORD", "test_password")
os.environ.setdefault("SECRET_KEY", "test_secret_key_at_least_32_chars_long")
os.environ.setdefault("BASE_URL", "http://localhost:8000")
os.environ.setdefault("SKIP_API_VALIDATION", "true")

from app import relevance  # noqa: E402
from app.models import (  # noqa: E402
    Account,
    Base,
    MediaMetadata,
    MovieTag,
    Tag,
    User,
    WatchEvent,
    WatchProgress,
)

TEST_DATABASE_URL = "sqlite:///./test_negative_signal.db"
engine = create_engine(TEST_DATABASE_URL, connect_args={"check_same_thread": False})
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture()
def db():
    Base.metadata.create_all(bind=engine)
    session = TestingSessionLocal()
    account = Account(
        auth_source="local",
        email="taste@example.com",
        password_hash="x",
        is_active=True,
    )
    session.add(account)
    session.commit()
    user = User(
        user_key="taste-key",
        account_id=account.id,
        auth_source="local",
        display_name="Taste",
        is_active=True,
    )
    session.add(user)
    session.commit()
    session.user_id = user.id  # type: ignore[attr-defined]
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


def _event(db, tmdb_id, action="complete", media_type="movie"):
    db.add(
        WatchEvent(
            user_id=db.user_id,
            tmdb_id=tmdb_id,
            media_type=media_type,
            action=action,
            created_at=datetime.utcnow(),
        )
    )
    db.commit()


def _progress(db, tmdb_id, ratio, media_type="movie", duration=6000, episode=None):
    db.add(
        WatchProgress(
            user_id=db.user_id,
            tmdb_id=tmdb_id,
            media_type=media_type,
            season=1 if episode else None,
            episode=episode,
            position=int(duration * ratio),
            duration=duration,
            updated_at=datetime.utcnow(),
        )
    )
    db.commit()


# ---------------------------------------------------------------------------
# completion_weights
# ---------------------------------------------------------------------------
def test_an_abandoned_title_scores_negative(db):
    """The whole point. 10-45% watched is a real signal, and a negative one."""
    _progress(db, 100, 0.25)

    weights = relevance.completion_weights(db, db.user_id)

    assert weights[100] == -0.4


def test_a_finished_title_scores_full_even_without_a_scrobble(db):
    """Clients miss the completion event; 85% watched is finishing it."""
    _progress(db, 100, 0.92)

    assert relevance.completion_weights(db, db.user_id)[100] == 1.0


def test_a_mis_click_is_neither_evidence_for_nor_against(db):
    _progress(db, 100, 0.03)

    assert 100 not in relevance.completion_weights(db, db.user_id)


def test_a_bulk_import_is_weaker_evidence_than_a_deliberate_watch(db):
    _event(db, 100, "complete")
    _event(db, 200, "imported")

    weights = relevance.completion_weights(db, db.user_id)

    assert weights[100] == 1.0
    assert weights[200] == 0.6


def test_same_numeric_movie_and_series_ids_do_not_share_affinity(db):
    """TMDB movie and TV IDs are separate namespaces."""
    _event(db, 100, "complete", media_type="movie")
    _event(db, 100, "imported", media_type="tv")

    assert relevance.completion_weights(db, db.user_id, "movie")[100] == 1.0
    assert relevance.completion_weights(db, db.user_id, "tv")[100] == 0.6


def test_a_short_sample_is_ignored_entirely(db):
    """Five seconds of a trailer says nothing about whether they liked it."""
    _progress(db, 100, 0.2, duration=120)

    assert 100 not in relevance.completion_weights(db, db.user_id)


def test_a_completion_is_never_overridden_by_a_later_partial_rewatch(db):
    """They finished it once. Sampling it again is not a dislike."""
    _event(db, 100, "complete")
    _progress(db, 100, 0.2)

    assert relevance.completion_weights(db, db.user_id)[100] == 1.0


def test_bailing_on_episode_one_is_not_yet_abandonment(db):
    """One episode is not enough to call it either way.

    A negative here would condemn every series a user sampled once, which is
    most of them.
    """
    _progress(db, 500, 0.2, media_type="tv", episode=1)

    assert 500 not in relevance.completion_weights(db, db.user_id)


def test_bailing_across_two_episodes_is_abandonment(db):
    _progress(db, 500, 0.2, media_type="tv", episode=1)
    _progress(db, 500, 0.15, media_type="tv", episode=2)

    assert relevance.completion_weights(db, db.user_id)[500] == -0.4


def test_a_finished_season_is_not_abandonment(db):
    """Averaging over episodes: one skipped episode inside a watched season
    must not read the same as bailing on the show."""
    for episode in range(1, 9):
        _progress(db, 500, 0.95, media_type="tv", episode=episode)
    _progress(db, 500, 0.05, media_type="tv", episode=9)

    assert relevance.completion_weights(db, db.user_id)[500] == 1.0


def test_a_user_with_no_history_gets_no_weights(db):
    assert relevance.completion_weights(db, db.user_id) == {}


# ---------------------------------------------------------------------------
# The effect on a taste vector
# ---------------------------------------------------------------------------
def _corpus(db):
    """Two tags. Slow-Burn is on the abandoned titles, Heist on the finished
    ones, and both appear on shared filler titles so neither is degenerate.

    ``weight_norm`` is L2-normalised per title, so a single-tag title always has
    ``weight_norm = 1.0`` whatever the tag's IDF -- a fixture that gave each
    title one tag would be measuring nothing.
    """
    tags = {}
    for name in ("Slow-Burn", "Heist", "Filler"):
        tag = Tag(
            name=name,
            category="pacing_format" if name == "Slow-Burn" else "plot_device",
        )
        db.add(tag)
        db.commit()
        tags[name] = tag

    def _title(tmdb_id, names):
        db.add(
            MediaMetadata(
                tmdb_id=tmdb_id,
                media_type="movie",
                title=f"T{tmdb_id}",
                popularity=10.0,
                vote_average=7.0,
                vote_count=500,
                release_date="2015-01-01",
            )
        )
        for name in names:
            db.add(
                MovieTag(
                    tmdb_id=tmdb_id,
                    media_type="movie",
                    tag_id=tags[name].id,
                    confidence=0.9,
                )
            )

    for i in range(1, 4):
        _title(i, ["Slow-Burn", "Filler"])
    for i in range(4, 7):
        _title(i, ["Heist", "Filler"])
    for i in range(7, 25):
        _title(i, ["Filler"])
    db.commit()
    relevance.refresh_tag_stats(db, create_indexes=False)
    return tags


def test_abandoning_a_subgenre_removes_it_from_the_profile(db):
    """Abandon three Slow-Burns, finish three Heists.

    Slow-Burn must not merely rank lower -- it must be gone. A vector that
    still carries it will keep producing Slow-Burn rows.
    """
    tags = _corpus(db)
    for tmdb_id in (1, 2, 3):
        _progress(db, tmdb_id, 0.25)
    for tmdb_id in (4, 5, 6):
        _event(db, tmdb_id, "complete")

    factors = relevance.completion_weights(db, db.user_id)
    vector = relevance.build_taste_vector(
        db, [(i, factors.get(i, 1.0)) for i in range(1, 7)], "movie"
    )

    assert tags["Heist"].id in vector
    assert tags["Slow-Burn"].id not in vector


def test_without_the_factors_the_abandoned_subgenre_survives(db):
    """The counterfactual: this is what the profile looked like before.

    Both subgenres carry equal weight, because "watched a bit of" and
    "finished" were the same input.
    """
    tags = _corpus(db)

    vector = relevance.build_taste_vector(db, list(range(1, 7)), "movie")

    assert tags["Heist"].id in vector
    assert tags["Slow-Burn"].id in vector


def test_a_negative_cancels_rather_than_inverting(db):
    """Negatives cancel positives inside the sum, and the vector is clamped at
    zero -- a tag can never rank *because* it was disliked."""
    tags = _corpus(db)
    _event(db, 1, "complete")  # one Slow-Burn liked
    for tmdb_id in (2, 3):
        _progress(db, tmdb_id, 0.2)  # two abandoned

    factors = relevance.completion_weights(db, db.user_id)
    vector = relevance.build_taste_vector(
        db, [(i, factors.get(i, 1.0)) for i in range(1, 4)], "movie"
    )

    assert all(weight > 0 for weight in vector.values())
    # 1.0 - 0.4 - 0.4 = 0.2 of a Slow-Burn contribution left: reduced, not
    # eliminated, and never negative.
    assert tags["Slow-Burn"].id in vector


def test_partial_watching_alone_builds_a_profile_with_no_events_at_all(db):
    """A user who never scrobbles still gets taste, from progress alone."""
    tags = _corpus(db)
    for tmdb_id in (4, 5, 6):
        _progress(db, tmdb_id, 0.95)

    factors = relevance.completion_weights(db, db.user_id)
    vector = relevance.build_taste_vector(
        db, [(i, f) for i, f in factors.items()], "movie"
    )

    assert tags["Heist"].id in vector
