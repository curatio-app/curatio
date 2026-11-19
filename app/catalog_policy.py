"""Home-catalog admission rules that must not affect intentional search.

Mockbusters are real, independently addressable works, so they stay in metadata
storage and exact-title search.  They are excluded only from recommendation and
timely Home shelves, where a deliberately confusing title/poster is otherwise
presented without the context a search query provides.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

# TMDB's canonical ``mockbuster`` keyword.
TMDB_MOCKBUSTER_KEYWORD_ID = 208879

# Existing movie discovery also excludes TMDB keyword 210024.  A pipe is TMDB's
# OR syntax, so a title carrying either keyword is omitted.
HOME_MOVIE_WITHOUT_KEYWORDS = f"210024|{TMDB_MOCKBUSTER_KEYWORD_ID}"

# Seed the two production incidents so stale/list-only metadata is safe before
# its keyword facts have been enriched.  These are movie IDs; TMDB's movie and
# TV namespaces are independent.
KNOWN_HOME_MOCKBUSTER_TMDB_IDS = frozenset({1698856, 1698863})

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _normalized_keyword(value: Any) -> str:
    """Return a provider-agnostic keyword token."""
    if isinstance(value, Mapping):
        value = value.get("name") or value.get("slug") or ""
    text = str(value or "").strip().casefold()
    if text.startswith("tmdb."):
        text = text.removeprefix("tmdb.")
    return _NON_ALNUM.sub("", text)


def _keyword_values(value: Any) -> Iterable[Any]:
    """Yield strings/dicts from TMDB, tag-fact, or MDBList keyword shapes."""
    if isinstance(value, Mapping):
        if value.get("id") is not None or value.get("name") or value.get("slug"):
            yield value
        for key in ("keywords", "results"):
            nested = value.get(key)
            if nested is not None:
                yield from _keyword_values(nested)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield from _keyword_values(item)
    elif value is not None:
        yield value


def keyword_payload_is_mockbuster(value: Any) -> bool:
    """Recognize TMDB id/name and MDBList's ``tmdb.mockbuster`` spelling."""
    for keyword in _keyword_values(value):
        if isinstance(keyword, Mapping):
            try:
                if int(keyword.get("id")) == TMDB_MOCKBUSTER_KEYWORD_ID:
                    return True
            except (TypeError, ValueError):
                pass
        if _normalized_keyword(keyword) == "mockbuster":
            return True
    return False


def is_home_mockbuster(
    metadata: Any | None,
    *,
    tmdb_id: int | None = None,
    media_type: str | None = None,
) -> bool:
    """Return whether a stored item is suppressed from Home catalogs only."""
    resolved_type = str(media_type or getattr(metadata, "media_type", "")).casefold()
    if resolved_type not in {"movie", ""}:
        return False

    resolved_id = tmdb_id if tmdb_id is not None else getattr(metadata, "tmdb_id", None)
    try:
        if int(resolved_id) in KNOWN_HOME_MOCKBUSTER_TMDB_IDS:
            return True
    except (TypeError, ValueError):
        pass

    if metadata is None:
        return False
    for field in ("tag_facts", "mdb_facts"):
        facts = getattr(metadata, field, None)
        if isinstance(facts, Mapping) and keyword_payload_is_mockbuster(
            facts.get("keywords")
        ):
            return True
    return False
