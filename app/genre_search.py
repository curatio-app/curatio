"""Genre search: "horror", "80s action movies", "scary shows", ranked by taste.

The tag-name search in ``app.main`` only fired for an exact tag name that
almost no title contains, so the queries people actually type for a genre --
"horror", "action movies", "sci fi shows" -- fell through to title matching and
returned films that merely had the word in their name.

This module reads such a query as a set of tag groups plus an optional media
type, then ranks the genre's titles in two stages:

1. **Candidates in SQL.** Titles carrying at least one tag from every group,
   behind the catalog quality floor, ordered by ``relevance x quality``. A
   group is a tag plus its sub-genres, so "horror" includes Folk Horror and
   Slasher titles rather than only the ones tagged with the root.
2. **Taste in Python.** The profile's stored taste vector re-ranks that pool,
   the same multiply-and-sum :func:`app.relevance.taste_scored_candidates`
   uses, so search agrees with the rows about what this profile likes.

Nothing here calls a model; it is a few indexed queries per search.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import case, func
from sqlalchemy.orm import Session

from app.catalog_generator import quality_expr, quality_floor_ids
from app.models import MediaMetadata, MovieTag, Tag, UserTasteVector, WatchEvent
from app.taxonomy import ALIAS_INDEX, normalize

#: Words that say which shelf to search, not what is on it.
_TYPE_WORDS = {
    "movie": "movie",
    "movies": "movie",
    "film": "movie",
    "films": "movie",
    "show": "tv",
    "shows": "tv",
    "series": "tv",
    "tv": "tv",
}

#: Words people put around a genre that carry no meaning for the match.
_FILLER = frozenset(
    {"good", "best", "great", "top", "some", "a", "an", "the", "of", "and", "with"}
)

#: Everyday wordings the taxonomy deliberately does not alias, because the
#: tagger never emits them. Search is where people do type them.
_SEARCH_ALIASES = {
    "scary": "Horror",
    "funny": "Comedy",
    "rom com": "Romantic Comedy",
    "romcom": "Romantic Comedy",
    "cartoon": "Animation",
    "cartoons": "Animation",
    "docs": "Documentary",
    "doc": "Documentary",
    "space": "Sci-Fi",
}

#: Longest phrase tried as one tag. "romantic comedy" and "true crime" are two
#: words; nothing useful is longer than three.
_MAX_PHRASE_WORDS = 3

#: Size of the SQL candidate pool the taste stage re-ranks.
CANDIDATE_POOL = 300

#: How far taste can lift a title. At 1.5 a perfect taste match scores 2.5x a
#: title with no shared taste, enough to reorder the genre without letting a
#: weak, barely-genre title outrank a strong one.
TASTE_LIFT = 1.5

#: Multiplier for a film the profile already finished: still findable, but no
#: longer presented as a suggestion.
WATCHED_PENALTY = 0.2


@dataclass
class GenreQuery:
    """A search parsed into tag groups.

    ``groups`` holds one list of tag ids per term the user typed: the matched
    tag first, then its sub-genres. A title must match every group.
    """

    tags: list[Tag]
    groups: list[list[int]]
    media_types: tuple[str, ...]
    #: True when the wording leaves no doubt the user means a genre: a type word
    #: ("horror movies"), an everyday alias ("scary"), several terms, or a
    #: broad root genre. Otherwise the caller applies its title-first checks.
    explicit: bool

    @property
    def label(self) -> str:
        return " ".join(t.name for t in self.tags)


@dataclass
class RankedTitle:
    tmdb_id: int
    media_type: str
    score: float
    quality: float
    watched: bool = False
    #: Names of the profile's taste tags this title shares, strongest first.
    reasons: list[str] = field(default_factory=list)
    #: Every tag id from the query groups the title carries.
    tag_ids: set[int] = field(default_factory=set)


def _decade_key(word: str) -> str | None:
    """ "80s" / "'80s" -> "1980s". Four-digit decades already resolve."""
    if len(word) == 3 and word.endswith("s") and word[:2].isdigit():
        century = "20" if word[0] in "012" else "19"
        return f"{century}{word[:2]}s"
    return None


def _resolve_phrase(phrase: str) -> tuple[str | None, bool]:
    """Map one phrase to a canonical tag name. Returns ``(name, via_alias)``."""
    if phrase in _SEARCH_ALIASES:
        return _SEARCH_ALIASES[phrase], True
    name = ALIAS_INDEX.get(phrase)
    if name:
        return name, False
    decade = _decade_key(phrase)
    if decade and ALIAS_INDEX.get(decade):
        return ALIAS_INDEX[decade], False
    # Plurals: "comedies", "thrillers", "westerns".
    for suffix, replacement in (("ies", "y"), ("s", "")):
        if phrase.endswith(suffix) and len(phrase) > len(suffix) + 2:
            singular = phrase[: -len(suffix)] + replacement
            if singular in _SEARCH_ALIASES:
                return _SEARCH_ALIASES[singular], True
            if ALIAS_INDEX.get(singular):
                return ALIAS_INDEX[singular], False
    return None, False


def _split_terms(words: list[str]) -> list[tuple[str, bool]] | None:
    """Cover every word with tag phrases, longest match first.

    Returns ``None`` when any word is left over: "american horror story" must
    stay a title search, not become Horror.
    """
    names: list[tuple[str, bool]] = []
    i = 0
    while i < len(words):
        for size in range(min(_MAX_PHRASE_WORDS, len(words) - i), 0, -1):
            name, via_alias = _resolve_phrase(" ".join(words[i : i + size]))
            if name:
                names.append((name, via_alias))
                i += size
                break
        else:
            return None
    return names


def _descendant_ids(db: Session, tag_id: int) -> list[int]:
    """The tag plus its sub-genres, two levels deep (the taxonomy's depth)."""
    ids = [tag_id]
    frontier = [tag_id]
    for _ in range(2):
        children = [
            int(r[0])
            for r in db.query(Tag.id)
            .filter(Tag.parent_id.in_(frontier), Tag.is_active.is_(True))
            .all()
        ]
        if not children:
            break
        ids.extend(children)
        frontier = children
    return ids


def parse_genre_query(db: Session, q: str) -> GenreQuery | None:
    """Read ``q`` as a genre search, or return ``None`` if it is not one."""
    words = normalize(q).split()
    media_types = {_TYPE_WORDS[w] for w in words if w in _TYPE_WORDS}
    words = [w for w in words if w not in _TYPE_WORDS and w not in _FILLER]
    if not words:
        return None

    names = _split_terms(words)
    if not names:
        return None

    tags: list[Tag] = []
    groups: list[list[int]] = []
    for name, _ in names:
        tag = (
            db.query(Tag)
            .filter(Tag.name == name, Tag.display.is_(True), Tag.is_active.is_(True))
            .first()
        )
        if tag is None:
            return None
        if tag.id in {t.id for t in tags}:
            continue
        tags.append(tag)
        groups.append(_descendant_ids(db, int(tag.id)))

    explicit = (
        bool(media_types)
        or any(via_alias for _, via_alias in names)
        or len(tags) > 1
        or any(t.category == "subgenre" and t.parent_id is None for t in tags)
    )
    # One type word narrows the search; "movies and shows" names both.
    types = tuple(media_types) if len(media_types) == 1 else ("movie", "tv")
    return GenreQuery(tags=tags, groups=groups, media_types=types, explicit=explicit)


def _candidates(
    db: Session, groups: list[list[int]], media_type: str, limit: int
) -> list[tuple[int, float, float]]:
    """``[(tmdb_id, relevance, quality)]`` matching every group."""
    group_of: dict[int, int] = {}
    for index, ids in enumerate(groups):
        for tag_id in ids:
            group_of.setdefault(tag_id, index)

    relevance = func.sum(MovieTag.weight_norm)
    quality = func.max(quality_expr())
    rows = (
        db.query(MovieTag.tmdb_id, relevance, quality)
        .join(
            MediaMetadata,
            (MediaMetadata.tmdb_id == MovieTag.tmdb_id)
            & (MediaMetadata.media_type == MovieTag.media_type),
        )
        .filter(
            MovieTag.media_type == media_type,
            MovieTag.tag_id.in_(list(group_of)),
            MovieTag.tmdb_id.in_(quality_floor_ids(db, media_type)),
        )
        .group_by(MovieTag.tmdb_id)
        .having(
            func.count(func.distinct(case(group_of, value=MovieTag.tag_id)))
            >= len(groups)
        )
        .order_by((relevance * quality).desc(), MovieTag.tmdb_id)
        .limit(limit)
        .all()
    )
    return [(int(r[0]), float(r[1] or 0.0), float(r[2] or 0.0)) for r in rows]


def taste_ranked(
    db: Session,
    query: GenreQuery,
    user_id: int | None,
    *,
    pool: int = CANDIDATE_POOL,
) -> list[RankedTitle]:
    """Rank the genre's titles for one profile, best first.

    Score = (half genre strength + half quality) x (1 + lift x taste). A
    profile without a taste vector gets the genre in quality order, and films
    it already finished sink to the bottom rather than vanish.
    """
    query_tag_ids = {tid for ids in query.groups for tid in ids}
    ranked: list[RankedTitle] = []
    for media_type in query.media_types:
        candidates = _candidates(db, query.groups, media_type, pool)
        if not candidates:
            continue
        ids = [c[0] for c in candidates]

        # Which query tags each title carries, for the "Because you like" rows.
        carried: dict[int, set[int]] = defaultdict(set)
        for tmdb_id, tag_id in (
            db.query(MovieTag.tmdb_id, MovieTag.tag_id)
            .filter(
                MovieTag.media_type == media_type,
                MovieTag.tmdb_id.in_(ids),
                MovieTag.tag_id.in_(list(query_tag_ids)),
            )
            .all()
        ):
            carried[int(tmdb_id)].add(int(tag_id))

        affinity: dict[int, float] = defaultdict(float)
        shared: dict[int, list[tuple[float, str]]] = defaultdict(list)
        if user_id is not None:
            for tmdb_id, contribution, name, tag_id in (
                db.query(
                    MovieTag.tmdb_id,
                    UserTasteVector.weight * MovieTag.weight_norm,
                    Tag.name,
                    Tag.id,
                )
                .join(
                    UserTasteVector,
                    (UserTasteVector.tag_id == MovieTag.tag_id)
                    & (UserTasteVector.media_type == MovieTag.media_type),
                )
                .join(Tag, Tag.id == MovieTag.tag_id)
                .filter(
                    UserTasteVector.user_id == user_id,
                    MovieTag.media_type == media_type,
                    MovieTag.tmdb_id.in_(ids),
                    Tag.display.is_(True),
                )
                .all()
            ):
                value = float(contribution or 0.0)
                affinity[int(tmdb_id)] += value
                # The genre itself is why the title is here, not a reason.
                if value > 0 and int(tag_id) not in {t.id for t in query.tags}:
                    shared[int(tmdb_id)].append((value, name))

        watched: set[int] = set()
        if user_id is not None and media_type == "movie":
            watched = {
                int(r[0])
                for r in db.query(WatchEvent.tmdb_id)
                .filter(
                    WatchEvent.user_id == user_id,
                    WatchEvent.media_type == "movie",
                    WatchEvent.tmdb_id.in_(ids),
                )
                .all()
            }

        top_relevance = max(c[1] for c in candidates) or 1.0
        top_affinity = max((v for v in affinity.values() if v > 0), default=0.0)
        for tmdb_id, relevance, quality in candidates:
            base = 0.5 * (relevance / top_relevance) + 0.5 * (quality / 10.0)
            taste = (
                max(affinity.get(tmdb_id, 0.0), 0.0) / top_affinity
                if top_affinity
                else 0.0
            )
            score = base * (1.0 + TASTE_LIFT * taste)
            is_watched = tmdb_id in watched
            if is_watched:
                score *= WATCHED_PENALTY
            ranked.append(
                RankedTitle(
                    tmdb_id=tmdb_id,
                    media_type=media_type,
                    score=score,
                    quality=quality,
                    watched=is_watched,
                    reasons=[
                        name
                        for _, name in sorted(shared.get(tmdb_id, ()), reverse=True)
                    ][:2],
                    tag_ids=carried.get(tmdb_id, set()),
                )
            )

    ranked.sort(key=lambda r: (-r.score, r.media_type, r.tmdb_id))
    return ranked


def favourite_subgenres(
    db: Session, query: GenreQuery, user_id: int | None, limit: int = 2
) -> list[Tag]:
    """The profile's best-liked sub-genres of the searched genre(s)."""
    if user_id is None:
        return []
    child_ids = {
        tid for ids in query.groups for tid in ids[1:]
    }  # ids[0] is the searched tag itself
    if not child_ids:
        return []
    weight = func.sum(UserTasteVector.weight)
    rows = (
        db.query(Tag, weight)
        .join(UserTasteVector, UserTasteVector.tag_id == Tag.id)
        .filter(
            UserTasteVector.user_id == user_id,
            UserTasteVector.media_type.in_(list(query.media_types)),
            Tag.id.in_(list(child_ids)),
            Tag.display.is_(True),
        )
        .group_by(Tag.id)
        .having(weight > 0)
        .order_by(weight.desc(), Tag.id)
        .limit(limit)
        .all()
    )
    return [r[0] for r in rows]
