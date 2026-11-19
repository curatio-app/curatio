"""Persistent season-level controls for intro and credits detection."""

from app.models import AdminSetting
from app.time_utils import utc_now


def segment_detection_setting_key(imdb_id: str, season: int, segment_type: str) -> str:
    return f"SEGMENT_DETECTION:{imdb_id}:{season}:{segment_type}"


def season_segment_enabled(db, imdb_id: str, season: int, segment_type: str) -> bool:
    row = db.get(
        AdminSetting,
        segment_detection_setting_key(imdb_id, season, segment_type),
    )
    return row is None or str(row.value).lower() not in {"false", "0", "off"}


def set_season_segment_enabled(
    db, imdb_id: str, season: int, segment_type: str, enabled: bool
) -> None:
    key = segment_detection_setting_key(imdb_id, season, segment_type)
    row = db.get(AdminSetting, key)
    if row is None:
        row = AdminSetting(key=key)
        db.add(row)
    row.value = "true" if enabled else "false"
    row.updated_at = utc_now()
