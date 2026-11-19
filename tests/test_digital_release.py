# ruff: noqa: E402
"""Tests for the digital (home) release date surfaced on details.

Covers the pure TMDB extractor (``TMDBClient.digital_release_date``) and its wiring
into the details item serializer (``_tmdb_detail_item``). Both are pure functions — no
network or DB — but importing the app modules needs the standard test env.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_digital_release.db")
os.environ.setdefault("TMDB_API_KEY", "test_key")
os.environ.setdefault("GEMINI_API_KEY", "test_key")
os.environ.setdefault("MASTER_PASSWORD", "test_password")
os.environ.setdefault("SECRET_KEY", "test_secret_key_at_least_32_chars_long")
os.environ.setdefault("BASE_URL", "http://localhost:8000")
os.environ.setdefault("SKIP_API_VALIDATION", "true")

from app.main import (  # noqa: E402
    _apply_detail_dynamic_overlay,
    _detail_dynamic_overlay,
    _title_extras,
    _tmdb_certification,
    _tmdb_detail_item,
)
from app.models import MediaMetadata  # noqa: E402
from app.tmdb_client import TMDBClient  # noqa: E402


def _region(iso: str, *entries: tuple[int, str]) -> dict:
    return {
        "iso_3166_1": iso,
        "release_dates": [
            {"type": rtype, "release_date": date} for rtype, date in entries
        ],
    }


class TestDigitalReleaseDate:
    def test_returns_us_digital_date_as_yyyy_mm_dd(self):
        data = {
            "results": [
                _region(
                    "US",
                    (3, "2024-05-01T00:00:00.000Z"),  # theatrical, ignored
                    (4, "2024-06-15T00:00:00.000Z"),  # digital
                ),
            ]
        }
        assert TMDBClient.digital_release_date(data) == "2024-06-15"

    def test_prefers_region_even_over_an_earlier_foreign_digital_date(self):
        data = {
            "results": [
                _region("DE", (4, "2024-05-01T00:00:00.000Z")),
                _region(
                    "US",
                    (4, "2024-08-01T00:00:00.000Z"),
                    (4, "2024-06-15T00:00:00.000Z"),  # earliest US type-4 wins
                ),
            ]
        }
        assert TMDBClient.digital_release_date(data) == "2024-06-15"

    def test_late_us_catalog_date_does_not_make_interstellar_new_again(self):
        """A retailer date years later is not the movie's digital debut."""
        data = {
            "results": [
                _region(
                    "US",
                    (3, "2014-11-07T00:00:00.000Z"),
                    (5, "2015-03-31T00:00:00.000Z"),
                    (4, "2026-08-03T00:00:00.000Z"),
                ),
                _region("BE", (4, "2014-11-07T00:00:00.000Z")),
            ]
        }
        assert TMDBClient.digital_release_date(data) == "2014-11-07"

    def test_late_us_catalog_date_is_suppressed_without_an_original_date(self):
        data = {
            "results": [
                _region(
                    "US",
                    (3, "2010-05-01T00:00:00.000Z"),
                    (4, "2026-08-03T00:00:00.000Z"),
                )
            ]
        }
        assert TMDBClient.digital_release_date(data) is None

    def test_falls_back_to_earliest_digital_date_anywhere_when_region_absent(self):
        data = {
            "results": [
                _region("GB", (4, "2024-07-20T00:00:00.000Z")),
                _region("DE", (4, "2024-07-10T00:00:00.000Z")),
            ]
        }
        assert TMDBClient.digital_release_date(data) == "2024-07-10"

    def test_none_when_no_digital_release(self):
        data = {"results": [_region("US", (3, "2024-05-01T00:00:00.000Z"))]}
        assert TMDBClient.digital_release_date(data) is None

    def test_none_for_empty_payload(self):
        assert TMDBClient.digital_release_date({}) is None
        assert TMDBClient.digital_release_date({"results": []}) is None

    def test_ignores_type_4_entries_without_a_date(self):
        data = {"results": [{"iso_3166_1": "US", "release_dates": [{"type": 4}]}]}
        assert TMDBClient.digital_release_date(data) is None


class TestDetailItemDigitalField:
    def _movie(self, release_dates: dict | None) -> dict:
        detail = {"id": 27205, "title": "Inception", "release_date": "2010-07-16"}
        if release_dates is not None:
            detail["release_dates"] = release_dates
        return detail

    def test_movie_with_digital_release_sets_field(self):
        detail = self._movie(
            {"results": [_region("US", (4, "2010-12-07T00:00:00.000Z"))]}
        )
        item = _tmdb_detail_item(detail, "movie")
        assert item["digitalReleaseInfo"] == "2010-12-07"

    def test_movie_without_digital_release_omits_field(self):
        detail = self._movie(
            {"results": [_region("US", (3, "2010-07-16T00:00:00.000Z"))]}
        )
        item = _tmdb_detail_item(detail, "movie")
        assert item["digitalReleaseInfo"] is None

    def test_movie_without_release_dates_key_omits_field(self):
        item = _tmdb_detail_item(self._movie(None), "movie")
        assert item["digitalReleaseInfo"] is None

    def test_series_never_gets_the_field(self):
        detail = {
            "id": 1399,
            "name": "Game of Thrones",
            "first_air_date": "2011-04-17",
            "release_dates": {
                "results": [_region("US", (4, "2011-06-01T00:00:00.000Z"))]
            },
        }
        item = _tmdb_detail_item(detail, "tv")
        assert item["digitalReleaseInfo"] is None

    def test_movie_content_rating_is_exposed(self):
        detail = self._movie(
            {
                "results": [
                    {
                        "iso_3166_1": "US",
                        "release_dates": [{"certification": "PG-13"}],
                    }
                ]
            }
        )
        assert _tmdb_certification(detail, "movie") == "PG-13"

    def test_series_content_rating_is_exposed(self):
        detail = {
            "id": 1399,
            "name": "Game of Thrones",
            "first_air_date": "2011-04-17",
            "content_ratings": {"results": [{"iso_3166_1": "US", "rating": "TV-MA"}]},
        }
        assert _tmdb_certification(detail, "tv") == "TV-MA"


def test_live_release_payload_can_clear_a_stale_stored_coming_soon_date():
    stored = MediaMetadata(
        tmdb_id=157336,
        media_type="movie",
        title="Interstellar",
        digital_release_date="2099-08-03",
    )
    live_detail = {
        "release_dates": {
            "results": [
                _region(
                    "US",
                    (3, "2014-11-07T00:00:00.000Z"),
                    (4, "2026-08-03T00:00:00.000Z"),
                )
            ]
        }
    }

    extras = _title_extras(stored, live_detail, "movie")

    assert "comingSoonDate" not in extras


def test_persisted_detail_snapshot_repairs_its_digital_date_on_read():
    payload = {
        "item": {
            "tmdb_id": 157336,
            "media_type": "movie",
            "title": "Interstellar",
            "digitalReleaseInfo": "2026-08-03",
        },
        "_provider_detail": {
            "release_dates": {
                "results": [
                    _region(
                        "US",
                        (3, "2014-11-07T00:00:00.000Z"),
                        (5, "2015-03-31T00:00:00.000Z"),
                        (4, "2026-08-03T00:00:00.000Z"),
                    ),
                    _region("BE", (4, "2014-11-07T00:00:00.000Z")),
                ]
            }
        },
    }

    repaired = _apply_detail_dynamic_overlay(payload, _detail_dynamic_overlay(payload))

    assert repaired["item"]["digitalReleaseInfo"] == "2014-11-07"
