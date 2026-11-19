from datetime import timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base, PlaybackPresence, PlaybackSourceRequest, User
from app.playback_activity import (
    PresenceReport,
    activity_snapshot,
    begin_request,
    finish_request,
    record_presence,
)
from app.time_utils import utc_now


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        session.add_all(
            [
                User(id=1, user_key="one", email="one@example.test"),
                User(id=2, user_key="two", email="two@example.test"),
            ]
        )
        session.commit()
        yield session
    engine.dispose()


def report(**changes):
    return PresenceReport(
        **{
            **dict(
                session_id=str(uuid4()),
                sequence=0,
                device_id="device-one",
                platform="ios",
                media_id="tt123:1:2",
                media_type="series",
                state="starting",
            ),
            **changes,
        }
    )


def test_independent_devices_and_actual_state_counts(db):
    now = utc_now()
    for device, state in (("one", "playing"), ("two", "paused")):
        record_presence(db, db.get(User, 1), report(device_id=device, state=state), now)
    data = activity_snapshot(db, now)
    assert data["counts"] == {"starting": 0, "playing": 1, "paused": 1, "buffering": 0}
    assert len(data["playback"]) == 2


def test_stale_is_not_playing_and_late_sequence_cannot_refresh_presence(db):
    now, session_id = utc_now(), str(uuid4())
    user = db.get(User, 1)
    record_presence(
        db, user, report(session_id=session_id, sequence=4, state="playing"), now
    )
    assert not record_presence(
        db, user, report(session_id=session_id, sequence=3), now + timedelta(seconds=50)
    )["accepted"]
    data = activity_snapshot(db, now + timedelta(seconds=60))
    assert data["playback"][0]["state"] == "stale"
    assert data["counts"]["playing"] == 0


@pytest.mark.parametrize("terminal", ["stopped", "ended", "failed"])
def test_terminal_session_cannot_be_revived(db, terminal):
    session_id = str(uuid4())
    user = db.get(User, 1)
    record_presence(db, user, report(session_id=session_id, state=terminal))
    assert not record_presence(
        db, user, report(session_id=session_id, sequence=10, state="playing")
    )["accepted"]
    assert activity_snapshot(db)["playback"][0]["state"] == terminal


def test_session_ownership_item_and_device_are_immutable(db):
    session_id = str(uuid4())
    record_presence(db, db.get(User, 1), report(session_id=session_id))
    for user_id, changes in (
        (2, {}),
        (1, {"media_id": "tt456"}),
        (1, {"device_id": "other"}),
    ):
        assert not record_presence(
            db,
            db.get(User, user_id),
            report(session_id=session_id, sequence=5, **changes),
        )["accepted"]


def test_recovery_updates_source_without_another_viewing_session(db):
    session_id = str(uuid4())
    user = db.get(User, 1)
    record_presence(
        db,
        user,
        report(
            session_id=session_id,
            state="playing",
            source_id="route-one",
            source_attempt=1,
        ),
    )
    record_presence(
        db,
        user,
        report(
            session_id=session_id,
            sequence=1,
            state="starting",
            source_id="route-two",
            source_attempt=2,
        ),
    )
    data = activity_snapshot(db)
    assert len(data["playback"]) == 1
    assert data["playback"][0]["source_attempt"] == 2
    assert data["playback"][0]["serving_provider"] is None


def test_request_starts_before_completion_and_never_counts_as_playback(db):
    now = utc_now()
    request_id = begin_request(
        db, user_key="one", media_id="tt123", media_type="movie", tier="low", now=now
    )
    data = activity_snapshot(db, now)
    assert data["requests"][0]["state"] == "searching"
    assert not data["playback"]
    finish_request(
        db,
        request_id,
        count=2,
        diagnostics={
            "cache_hit": True,
            "errors": [
                {
                    "title": "Addon timeout",
                    "description": "https://secret.example/token",
                }
            ],
        },
        now=now + timedelta(seconds=2),
    )
    data = activity_snapshot(db, now + timedelta(seconds=2))
    row = data["requests"][0]
    assert row["elapsed_ms"] == 2000 and row["result_count"] == 2 and row["cache_hit"]
    assert row["errors"] == ["Addon timeout"]
    assert "secret.example" not in str(data)


def test_old_records_are_hidden_then_purged(db):
    now = utc_now()
    record_presence(
        db, db.get(User, 1), report(state="playing"), now - timedelta(minutes=16)
    )
    begin_request(
        db,
        media_id="tt1",
        media_type="movie",
        tier="low",
        kind="test",
        now=now - timedelta(hours=25),
    )
    assert not activity_snapshot(db, now)["playback"]
    assert db.query(PlaybackSourceRequest).count() == 0
    assert db.query(PlaybackPresence).count() == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"source_id": "https://signed.example/private"},
        {"position": float("nan")},
        {"duration": float("inf")},
        {"sequence": -1},
        {"media_id": "https://secret"},
        {"headers": {"Authorization": "secret"}},
    ],
)
def test_transport_data_and_invalid_values_are_rejected(changes):
    with pytest.raises(ValidationError):
        report(**changes)
