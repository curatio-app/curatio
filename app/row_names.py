"""The one place a personal row's display name is decided.

Row names used to be hardcoded string literals in three files that nobody kept
in step, and the result was the complaint that started this: **seven pairs of
rows shared a name**. ``catalog_configs`` gave the movie slot and the series
slot of every feed family one string -- "Trending Now", "New Releases",
"Coming Soon", "Popular" -- and the taste families did the same for
"Recommended For You", "Top Picks For You" and "Hidden Gems For You". A
catalog is identified by ``(type, id)``, so nothing mis-routes, but on the web
home page every row is flattened into one list and the shelf header renders
``row.name`` alone, so a viewer sees "Coming Soon" twice with no way to tell which is which.

The data needed to disambiguate was already there and thrown away:
``catalog_configs`` carried a per-row media label that only ever reached a log
line.

Two rules this module exists to enforce:

* **A name says what kind of thing is in the row.** "Trending Movies" and
  "Trending Shows", never two "Trending Now".
* **One definition, two consumers.** The nightly sync writes these names onto
  ``user_catalogs``; a bootstrap copy once covered a profile that had not
  synced yet. Those were separate hand-maintained lists, so a rename in one
  silently disagreed with the other. Both now read this table.
"""

from __future__ import annotations

from typing import NamedTuple


class RowSpec(NamedTuple):
    """A personal row: its slot id, its display name, and its media type."""

    slot_id: str
    name: str
    media_type: str
    generation_method: str


def _typed(family: str, movie_name: str, series_name: str, method: str):
    """The movie and series halves of one family, named distinctly."""
    return (
        RowSpec(f"{family}-movie", movie_name, "movie", method),
        RowSpec(f"{family}-series", series_name, "tv", method),
    )


#: The TMDB-sourced feed rows, in no particular order (SLOT_ORDER decides that).
#:
#: "Shows" rather than "Series" throughout: it is the word a viewer uses, and
#: it keeps the movie and series labels a similar length so the two shelves
#: read as siblings rather than as unrelated rows.
TIMELY_FEED_ROWS: tuple[RowSpec, ...] = (
    *_typed("trending", "Trending Movies", "Trending Shows", "tmdb_trending"),
    *_typed("new-releases", "New Movies", "New Shows", "tmdb_new_releases"),
)

#: All feed definitions remain available to migration/compatibility code, but
#: only the timely rows above are part of the current catalog lineup. Popular
#: substantially repeats New Releases, while Coming Soon is intentionally not
#: restored as part of the timely-shelf contract.
FEED_ROWS: tuple[RowSpec, ...] = (
    *TIMELY_FEED_ROWS,
    *_typed(
        "coming-soon", "Movies Coming Soon", "Shows Coming Soon", "tmdb_coming_soon"
    ),
    *_typed("popular", "Popular Movies", "Popular Shows", "tmdb_popular"),
)

#: The zero-history bootstrap rows, shown only until a profile has watched
#: something.
DISCOVERY_ROWS: tuple[RowSpec, ...] = (
    *_typed("discover", "Discover Movies", "Discover Shows", "discovery_daily"),
    RowSpec(
        "discover-acclaimed", "Critically Acclaimed", "movie", "discovery_acclaimed"
    ),
)

#: The taste-derived rows. Their contents are a function of the viewer, so the
#: names say so.
TASTE_ROWS: tuple[RowSpec, ...] = (
    *_typed("rec", "Recommended Movies", "Recommended Shows", "tag_recommendations"),
    *_typed("picks", "Top Movie Picks", "Top Show Picks", "top_picks"),
    *_typed("gems", "Hidden Movie Gems", "Hidden Show Gems", "hidden_gems"),
)

_BY_SLOT: dict[str, RowSpec] = {
    spec.slot_id: spec for spec in (*FEED_ROWS, *DISCOVERY_ROWS, *TASTE_ROWS)
}


def row_name(slot_id: str, default: str = "") -> str:
    """Display name for a slot, or ``default`` for a slot not in this table.

    Byw and taste-* rows derive their names from a seed title or a tag, so they
    are deliberately absent here rather than given a placeholder.
    """
    spec = _BY_SLOT.get(slot_id)
    return spec.name if spec else default
