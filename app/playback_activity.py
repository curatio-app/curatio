"""Observed player presence and lookup activity, shared by every server worker."""

from datetime import timedelta
import re
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import and_, or_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.models import PlaybackPresence, PlaybackSourceRequest, User
from app.time_utils import utc_now

TERMINAL = ("stopped", "ended", "failed")
ACTIVE = ("starting", "playing", "buffering", "paused")
STALE_SECONDS = 60


def safe_label(value, limit=240):
    """Activity is display metadata, never a transport/debug payload."""
    text = str(value or "")
    # Any scheme://... value, not just http(s): app deep links carry tokens too.
    text = re.sub(r"(?i)\b[a-z][a-z0-9+.-]*://\S+", "[redacted URL]", text)
    text = re.sub(
        r"(?i)(authorization|password|token|secret)\s*[:=]\s*\S+",
        r"\1=[redacted]",
        text,
    )
    return " ".join(text.split())[:limit]


class PresenceReport(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    session_id: UUID
    profile_id: int | None = Field(default=None, ge=1)
    sequence: int = Field(ge=0, le=2147483647)
    device_id: str = Field(min_length=1, max_length=128, pattern=r"^[\w.:-]+$")
    platform: str = Field(min_length=1, max_length=40, pattern=r"^[\w .-]+$")
    media_id: str = Field(min_length=1, max_length=160, pattern=r"^[\w:.-]+$")
    media_type: Literal["movie", "series"]
    title: str | None = Field(default=None, max_length=500)
    season: int | None = Field(default=None, ge=0)
    episode: int | None = Field(default=None, ge=0)
    tier: Literal["low", "high"] | None = None
    state: Literal[
        "starting", "playing", "buffering", "paused", "stopped", "ended", "failed"
    ]
    position: float | None = Field(default=None, ge=0)
    duration: float | None = Field(default=None, ge=0)
    source_id: str | None = Field(default=None, max_length=256, pattern=r"^[\w:.-]+$")
    selected_provider: str | None = Field(default=None, max_length=100)
    source_attempt: int = Field(default=0, ge=0, le=100000)

    @field_validator("title", "selected_provider")
    @classmethod
    def sanitize_labels(cls, value):
        return safe_label(value) if value is not None else None


def prune_activity(db, now):
    cutoff = now - timedelta(hours=24)
    db.query(PlaybackPresence).filter(PlaybackPresence.last_seen < cutoff).delete(
        synchronize_session=False
    )
    db.query(PlaybackSourceRequest).filter(
        PlaybackSourceRequest.started_at < cutoff
    ).delete(synchronize_session=False)


def record_presence(db, user, payload: PresenceReport, now=None):
    now = now or utc_now()
    details = payload.model_dump(
        mode="json",
        exclude={
            "session_id",
            "profile_id",
            "sequence",
            "device_id",
            "media_id",
            "state",
        },
    )
    details["viewer"] = safe_label(
        user.display_name or user.email or f"User #{user.id}"
    )
    stored_tier = (
        user.bandwidth_tier if user.bandwidth_tier in ("low", "high") else "high"
    )
    details["tier"] = payload.tier or stored_tier
    # No final-provider claim: opaque redirects can select another provider.
    details["serving_provider"] = None
    values = dict(
        session_id=str(payload.session_id),
        user_id=user.id,
        device_id=payload.device_id,
        media_id=payload.media_id,
        sequence=payload.sequence,
        state=payload.state,
        details=details,
        started_at=now,
        last_seen=now,
    )
    insert = pg_insert if db.bind.dialect.name == "postgresql" else sqlite_insert
    stmt = insert(PlaybackPresence).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[PlaybackPresence.session_id],
        set_={
            key: getattr(stmt.excluded, key)
            for key in ("sequence", "state", "details", "last_seen")
        },
        where=and_(
            PlaybackPresence.user_id == user.id,
            PlaybackPresence.device_id == payload.device_id,
            PlaybackPresence.media_id == payload.media_id,
            PlaybackPresence.sequence < payload.sequence,
            PlaybackPresence.state.notin_(TERMINAL),
        ),
    )
    accepted = db.execute(stmt).rowcount == 1
    # Indexed, bounded retention; the live view also prunes idle installations.
    if payload.sequence % 60 == 0:
        prune_activity(db, now)
    db.commit()
    return {"status": "ok", "accepted": accepted}


def begin_request(
    db,
    *,
    user_key=None,
    media_id,
    media_type,
    tier,
    title=None,
    session_id=None,
    kind="playback",
    now=None,
):
    now = now or utc_now()
    user = (
        db.query(User).filter(User.user_key == user_key).first() if user_key else None
    )
    request_id = str(uuid4())
    db.add(
        PlaybackSourceRequest(
            request_id=request_id,
            user_id=user.id if user else None,
            session_id=str(session_id) if session_id else None,
            state="searching",
            started_at=now,
            details={
                "media_id": safe_label(media_id, 160),
                "media_type": media_type,
                "tier": tier,
                "title": safe_label(title) or None,
                "kind": kind,
                "viewer": (
                    safe_label(user.display_name or user.email or f"User #{user.id}")
                    if user
                    else "Admin test"
                ),
            },
        )
    )
    db.commit()
    return request_id


def finish_request(db, request_id, *, count=0, diagnostics=None, error=None, now=None):
    row = db.get(PlaybackSourceRequest, request_id)
    if not row:
        return
    diagnostics = diagnostics or {}
    row.state = (
        "failed"
        if error or diagnostics.get("status") in ("failed", "error", "timeout")
        else "ready"
    )
    row.finished_at = now or utc_now()
    # Upstream descriptions can contain credentialed URLs: store safe titles only.
    errors = diagnostics.get("errors") or []
    row.details = {
        **row.details,
        "result_count": count,
        "cache_hit": bool(diagnostics.get("cache_hit")),
        "errors": [
            safe_label(e.get("title", "Addon error") if isinstance(e, dict) else e)
            for e in errors[:30]
        ],
        "error": safe_label(error) or None,
    }
    db.commit()


def record_delegated_request(db, **context):
    """Older browser-direct discovery is observed only as a delegated request."""
    request_id = begin_request(db, **context)
    row = db.get(PlaybackSourceRequest, request_id)
    row.state = "delegated"
    row.finished_at = utc_now()
    db.commit()


def activity_snapshot(db, now=None):
    now = now or utc_now()
    prune_activity(db, now)
    recent = now - timedelta(minutes=15)
    rows = (
        db.query(PlaybackPresence)
        .filter(PlaybackPresence.last_seen >= recent)
        .order_by(PlaybackPresence.last_seen.desc())
        .limit(500)
        .all()
    )
    playback = []
    counts = {state: 0 for state in ACTIVE}
    for row in rows:
        age = max(0, (now - row.last_seen).total_seconds())
        state = "stale" if age >= STALE_SECONDS and row.state in ACTIVE else row.state
        if state in counts:
            counts[state] += 1
        playback.append(
            {
                **row.details,
                "session_id": row.session_id,
                "device_id": row.device_id,
                "media_id": row.media_id,
                "state": state,
                "reported_state": row.state,
                "last_seen": row.last_seen.isoformat() + "Z",
                "ago_seconds": int(age),
            }
        )
    requests = []
    for row in (
        db.query(PlaybackSourceRequest)
        .filter(
            or_(
                PlaybackSourceRequest.started_at >= recent,
                PlaybackSourceRequest.finished_at >= recent,
            )
        )
        .order_by(PlaybackSourceRequest.started_at.desc())
        .limit(200)
        .all()
    ):
        state = row.state
        elapsed = ((row.finished_at or now) - row.started_at).total_seconds()
        if state == "searching" and elapsed >= STALE_SECONDS:
            state = "stale"
        requests.append(
            {
                **row.details,
                "request_id": row.request_id,
                "session_id": row.session_id,
                "state": state,
                "elapsed_ms": round(elapsed * 1000),
                "started_at": row.started_at.isoformat() + "Z",
            }
        )
    db.commit()
    return {
        "playback": playback,
        "requests": requests,
        "counts": counts,
        "updated_at": now.isoformat() + "Z",
    }
