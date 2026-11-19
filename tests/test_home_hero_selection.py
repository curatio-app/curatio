import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TMDB_API_KEY", "test_key")
os.environ.setdefault("MASTER_PASSWORD", "test_password")
os.environ.setdefault("SECRET_KEY", "test_secret_key_at_least_32_chars_long")
os.environ.setdefault("BASE_URL", "http://localhost:8000")
os.environ.setdefault("SKIP_API_VALIDATION", "true")

from app.home_feed import HERO_ITEMS, HERO_MIN_VOTES, _select_hero  # noqa: E402


def _item(tmdb_id, *, logo=True, backdrop=True, votes=HERO_MIN_VOTES):
    return {
        "tmdb_id": tmdb_id,
        "media_type": "movie",
        "logo": f"/logo{tmdb_id}.png" if logo else None,
        "backdrop": f"/bd{tmdb_id}.jpg" if backdrop else None,
        "_votes": votes,
    }


def _row(row_id, items, media_type="movie"):
    return {"id": row_id, "name": row_id, "media_type": media_type, "items": items}


def test_hero_takes_only_logo_backdrop_well_known_titles_from_top_picks():
    picks = _row(
        "personal-picks-movie",
        [
            _item(1),
            _item(2, logo=False),
            _item(3, votes=12),
            _item(4, backdrop=False),
            _item(5),
            _item(6),
        ],
    )
    row, members = _select_hero([picks])
    assert row is picks
    assert [m["tmdb_id"] for m in members] == [1, 5, 6]


def test_obscure_top_picks_yield_to_trending():
    picks = _row("personal-picks-movie", [_item(i, votes=40) for i in range(1, 10)])
    trending = _row("universal-trending-movie", [_item(i) for i in range(100, 120)])
    row, members = _select_hero([picks, trending])
    assert row is trending
    assert len(members) == HERO_ITEMS


def test_logo_only_is_the_fallback_before_text_titles():
    picks = _row("personal-picks-movie", [_item(i, votes=10) for i in range(1, 5)])
    row, members = _select_hero([picks])
    assert row is picks
    assert all(m["logo"] for m in members)


def test_rows_without_any_logos_keep_the_previous_rule():
    picks = _row("personal-picks-movie", [_item(i, logo=False) for i in range(1, 5)])
    row, members = _select_hero([picks])
    assert row is picks
    assert [m["tmdb_id"] for m in members] == [1, 2, 3, 4]


def test_tv_rows_are_never_preferred_but_remain_the_last_resort():
    shows = _row("universal-trending-series", [_item(i) for i in range(1, 5)], "series")
    row, members = _select_hero([shows])
    assert row is shows
    assert _select_hero([]) == (None, [])
