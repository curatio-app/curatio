"""Synthesized catalog rows from tag co-occurrence.

The 40 curated categories are editorial: a human decided that "Dark & Gritty
Crime Dramas" is a row worth having. That does not scale, and it cannot know
what this particular library actually contains -- if the collection is thick
with Korean revenge thrillers, no hand-written list will say so.

This finds the combinations that are *over-represented* relative to chance and
turns them into rows. Two tags co-occurring often is not interesting on its own:
Drama co-occurs with everything. What matters is **lift** --

    lift(a, b) = P(a and b) / (P(a) * P(b))

-- which is 1.0 when two tags are independent and rises when seeing one makes
the other more likely than the corpus baseline. A lift of 1.8 means the pair
appears 80% more often than chance, which is the point at which the combination
reads as a genre rather than a coincidence.

Everything here runs at build time or weekly. The co-occurrence self-join is
minutes on a multi-million-row ``movie_tags``, and browse and catalog reads
continue to hit the pre-computed snapshots, so nothing on a request path ever
touches it.

**Names come from templates, not from a model.** ``taxonomy.NAME_TEMPLATES`` is
keyed by facet pair, so ("mood", "subgenre") produces "Gritty Neo-Noirs" with
zero API calls. That is the shippable path; an LLM polish pass can improve on it
later, but nothing depends on one.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from loguru import logger
from sqlalchemy import and_, func
from sqlalchemy.orm import Session, aliased

from app.catalog_generator import CatalogGenerator, _quality_value, quality_floor_ids
from app.models import (
    MediaMetadata,
    MovieTag,
    Tag,
    UniversalCatalogContent,
    UniversalCategory,
)
from app.config import settings
from app.shelf_editor import EditorialResult, review_candidates
from app.taxonomy import BY_NAME, NAME_TEMPLATES, period_phrase, pluralise
from app.time_utils import utc_now

#: A row nobody can fill is not a row. 60 titles is the floor at which a shelf
#: is worth rendering -- a fraction of the corpus, not a count, would make this
#: meaningless on a small library and unreachable on a large one.
MIN_ROW_SUPPORT = 60

#: Candidate tags sit between these bounds, expressed as fractions of the corpus
#: so they re-derive correctly at any library size. Below the floor a tag is too
#: rare to fill a row; above the ceiling it is too common to be specific --
#: "Drama" on 40% of the library says nothing about any title that carries it.
MIN_DOC_FRACTION = 0.0005
MAX_DOC_FRACTION = 0.20

#: A pair seen fewer times than this is noise, whatever its lift: three titles
#: sharing two rare tags produces an enormous lift and an empty shelf.
MIN_PAIR_SUPPORT = 40

#: 1.0 is independence. 1.8 is the point where a pair reads as a genre.
MIN_LIFT = 1.8

#: No tag may anchor more than this many rows, or a single dominant tag
#: (Thriller, say) crowds out every other combination in the greedy pass.
#:
#: Lowered from 3 after the first production preview: at 3 the output carried
#: three Cyberpunk rows and two Possession rows, which reads as repetition
#: rather than variety even though every one cleared the lift threshold.
MAX_ROWS_PER_TAG = 2

#: Only the ~250 most promising candidates enter the self-join. Without the
#: pre-filter the pair query is O(tags^2) over the whole vocabulary.
MAX_CANDIDATE_TAGS = 250

#: Pairs are counted over meaningful assignments only. A tag holding 2% of a
#: title's vector is not what the title is about.
MIN_PAIR_WEIGHT_NORM = 0.15

SYNTH_VERSION = "2.1.0"

# A discovery shelf must fill more than one useful viewport after every real
# quality/tag filter, not merely have encouraging co-occurrence support.
MIN_PUBLISHABLE_ITEMS = 30
VISIBLE_SHELF_ITEMS = 40
SYNTH_CANDIDATE_POOL = 200
MAX_TOP40_JACCARD = 0.70
MIN_EDITORIAL_NAME_LENGTH = 4
MAX_EDITORIAL_NAME_LENGTH = 45
MIN_EDITORIAL_KEEP_SCORE = 75

# Some tags describe a useful attribute on an individual title but produce a
# low-value shelf when used as the collection's premise. In particular,
# made-for-TV rows were consistently a wall of interchangeable catalog filler
# even after the normal title quality floor.
_LOW_VALUE_SHELF_TAGS = frozenset({"Made for Television"})

# A setting or plot device is the memorable premise of a two-tag shelf. Two
# rows using the same one (Detective Crime + Detective Mystery, or Sci-Fi in
# Outer Space + Outer-Space Adventure) read as duplicates even when their
# top-40 membership does not cross the Jaccard threshold.
_CONCEPT_DEDUPE_FACETS = frozenset(
    {
        "pacing_format",
        "place",
        "plot_device",
        "setting_period",
        "source_franchise",
        "style",
    }
)

_FAMILY_TAGS = frozenset({"Kids", "Family", "Family Movie Night", "All Ages"})
_MATURE_CERTIFICATIONS = frozenset(
    {"R", "NC-17", "X", "TV-MA", "18", "18+", "R18", "R18+"}
)
_FORMAT_TAGS = frozenset(
    {"Documentary", "Stand-Up Special", "Live Performance", "Concert Film"}
)
_MUSIC_FORMAT_TAGS = frozenset({"Music", "Musical", "Concert Film", "Live Performance"})

# Product-copy nouns are not taxonomy concepts. Every other canonical concept
# found in an AI name must be present in the deterministic name/tags.
_GENERIC_NAME_WORDS = frozenset(
    {
        "collection",
        "favorite",
        "favorites",
        "film",
        "films",
        "movie",
        "movies",
        "series",
        "show",
        "shows",
        "story",
        "stories",
        "case",
        "cases",
        "file",
        "files",
    }
)
_SHELF_SAFETY_TAGS = frozenset(
    set(_FORMAT_TAGS)
    | {name for name, definition in BY_NAME.items() if definition.maturity >= 3}
)
_LOOKUP_CHUNK_SIZE = 10_000

# Generated rows are data-driven, but their titles are product copy.  Broad
# genres such as Action and Music are attributive nouns in English: people say
# "Action Movies" and "Music Shows", not "Actions" and "Musics".  The generic
# taxonomy inflector cannot know that distinction, or whether the row contains
# movies or shows, so the synthesis layer owns these media-aware labels.
_MEDIA_LABELS = {"movie": "Movies", "tv": "Shows"}

_SUBGENRE_COLLECTIONS: dict[str, str] = {
    "Action": "Action {media}",
    "Adventure": "Adventure {media}",
    "Animation": "Animated {media}",
    "Crime": "Crime {media}",
    "Family": "Family {media}",
    "Fantasy": "Fantasy {media}",
    "History": "Historical {media}",
    "Horror": "Horror {media}",
    "Made for Television": "Made-for-TV Movies",
    "Martial Arts": "Martial Arts {media}",
    "Mecha": "Mecha {media}",
    "Music": "Music {media}",
    "Romance": "Romance {media}",
    "Sci-Fi": "Sci-Fi {media}",
    "Short Film": "Short Films",
    "Slice of Life": "Slice-of-Life {media}",
    "Superhero": "Superhero {media}",
    "War": "War {media}",
}

_STYLE_MODIFIERS: dict[str, str] = {
    "Black and White": "Black-and-White",
    "CG Animation": "CG-Animated",
    "Stop-Motion": "Stop-Motion Animated",
}

_PACING_MODIFIERS: dict[str, str] = {
    # This taxonomy tag means running time, not narrative scale. Calling the
    # result merely "Epic" made a long Indian drama look like a spectacle
    # shelf and allowed the editor to preserve the same misleading wording.
    "Epic Runtime": "Epic-Length",
    "Multi-Season Saga": "Long-Running",
    "Under 90 Minutes": "Short",
}

_PLOT_MODIFIERS: dict[str, str] = {
    "Hostage Crisis": "Hostage",
    "Love Triangle": "Love-Triangle",
    "Serial Killer": "Serial-Killer",
    "Time Travel": "Time-Travel",
}

# These pairs assert the same idea twice rather than creating a useful browse
# intersection.  High lift is expected for them, but that is taxonomy
# correlation, not an editorial discovery.
_REDUNDANT_PAIRS: frozenset[frozenset[str]] = frozenset(
    {
        frozenset({"Romantic", "Romance"}),
        frozenset({"Short Film", "Under 90 Minutes"}),
    }
)

# ``universal_categories.id`` and its content-table foreign key are VARCHAR(50)
# in every production database. Generated ids used to truncate the name to 44
# characters *before* adding ``gen-movie-`` (10 more), so perfectly valid long
# row names rolled back the whole synthesis transaction in PostgreSQL.
CATEGORY_ID_MAX_LENGTH = 50


@dataclass(frozen=True)
class RowSpec:
    """One proposed row, before it becomes a database record."""

    slug: str
    name: str
    media_type: str
    tag_a: str
    tag_b: str
    facet_a: str
    facet_b: str
    support: int
    lift: float

    @property
    def formula(self) -> dict[str, Any]:
        from workers.tagging_common import tagger_version

        return {
            "required": [self.tag_a, self.tag_b],
            "min_required": 2,
            "min_weight_norm": MIN_PAIR_WEIGHT_NORM,
            "tagger_version": tagger_version(),
        }

    @property
    def spec_hash(self) -> str:
        """Identity of the *specification*, not of the row.

        A name is only regenerated when this changes, so an LLM polish pass
        costs nothing on a re-run that found the same combinations.
        """
        raw = f"{SYNTH_VERSION}|{self.media_type}|{self.tag_a}|{self.tag_b}"
        return hashlib.sha1(raw.encode()).hexdigest()[:40]


def _slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "row"


def _generated_slug(media_type: str, name: str) -> str:
    """Stable, collision-safe generated id within the database's 50-char key."""
    prefix = f"gen-{media_type}-"
    available = CATEGORY_ID_MAX_LENGTH - len(prefix)
    slug = _slugify(name)
    if len(slug) > available:
        digest = hashlib.sha1(f"{media_type}|{name}".encode()).hexdigest()[:8]
        stem = slug[: available - len(digest) - 1].rstrip("-") or "row"
        slug = f"{stem}-{digest}"
    return f"{prefix}{slug}"


def _tokens(name: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", name.lower()) if t}


def _render(template: str, a: str, b: str) -> str:
    """Fill a name template, collapsing any doubled whitespace it produces."""
    rendered = template.format(
        a=a,
        b=b,
        **{
            "as": pluralise(a),
            "bs": pluralise(b),
            "ap": period_phrase(a),
            "bp": period_phrase(b),
        },
    )
    return re.sub(r"\s+", " ", rendered).strip()


def _media_label(media_type: str | None) -> str:
    """The noun a synthesized row is a collection of."""
    return _MEDIA_LABELS.get(media_type or "", "Titles")


def _subgenre_collection(tag: str, media_type: str | None) -> str:
    """Render a genre as a collection instead of blindly pluralising it."""
    template = _SUBGENRE_COLLECTIONS.get(tag)
    if template:
        return template.format(media=_media_label(media_type))
    return pluralise(tag)


def _source_collection(tag: str, media_type: str | None) -> str:
    media = _media_label(media_type)
    if tag == "Based on a Book":
        return "Book Adaptations"
    if tag == "Based on a True Story":
        return "True-Story " + media
    if tag == "Comic Book Adaptation":
        return "Comic Book Adaptations"
    if tag == "Manga Adaptation":
        return "Manga Adaptations"
    if tag == "Sequel":
        return "Sequels"
    return pluralise(tag)


def _standalone_collection(tag: str, facet: str, media_type: str | None) -> str:
    """Name a contained pair using its more specific tag exactly once."""
    media = _media_label(media_type)
    if facet == "subgenre":
        return _subgenre_collection(tag, media_type)
    if facet == "source_franchise":
        return _source_collection(tag, media_type)
    if facet == "setting_period":
        return f"{tag} {media}"
    if facet == "style":
        return f"{_STYLE_MODIFIERS.get(tag, tag)} {media}"
    if tag == "Adult Animation":
        return f"Adult Animated {media}"
    if tag == "Family Movie Night":
        return "Family Movie Night"
    return pluralise(tag)


def _pair_values(
    tag_a: str,
    facet_a: str,
    tag_b: str,
    facet_b: str,
    left_facet: str,
    right_facet: str,
) -> tuple[str, str] | None:
    """Return tags in a requested facet order, whichever order they arrived."""
    if (facet_a, facet_b) == (left_facet, right_facet):
        return tag_a, tag_b
    if (facet_b, facet_a) == (left_facet, right_facet):
        return tag_b, tag_a
    return None


def _editorial_name(
    tag_a: str,
    facet_a: str,
    tag_b: str,
    facet_b: str,
    media_type: str,
) -> tuple[str, str, str] | None:
    """Polish common pair relationships using media-aware product language.

    Lift decides *which* concepts deserve rows.  This function only decides
    how a person should read those concepts.  It is intentionally deterministic
    and local: regenerating catalogs never spends model tokens.
    """
    media = _media_label(media_type)
    pair = frozenset({tag_a, tag_b})

    # A few relationships need semantic copy rather than a facet-wide template.
    exact: dict[frozenset[str], str] = {
        frozenset({"Adult Animation", "Animation"}): f"Adult Animated {media}",
        frozenset({"CG Animation", "Family"}): f"CG-Animated Family {media}",
        frozenset({"CG Animation", "Family Movie Night"}): (
            f"CG-Animated Family {media}"
        ),
        frozenset({"Christmas", "Made for Television"}): (
            "Made-for-TV Christmas Movies"
        ),
        frozenset({"Coming of Age", "High School"}): (
            f"High-School Coming-of-Age {media}"
        ),
        frozenset({"Crime", "Heist"}): f"Heist {media}",
        frozenset({"Family", "Family Movie Night"}): "Family Movie Night",
        frozenset({"Family", "Kids"}): f"Family {media} for Kids",
        frozenset({"Friendship", "High School"}): (f"High-School Friendship {media}"),
        frozenset({"History", "World War II"}): (f"World War II Historical {media}"),
        frozenset({"Manga Adaptation", "Slice of Life"}): (
            "Slice-of-Life Manga Adaptations"
        ),
        frozenset({"Outer Space", "Adventure"}): (f"Outer-Space Adventure {media}"),
        frozenset({"Stop-Motion", "Animation"}): (f"Stop-Motion Animated {media}"),
        frozenset({"War", "World War II"}): f"World War II {media}",
    }
    if pair in exact:
        return exact[pair], tag_a, tag_b

    # A specific tag already containing the other concept should stand alone:
    # Christmas + Christmas Movie is "Christmas Movies", never "Christmas
    # Christmas Movies".  Exact pairs above run first because Family + Family
    # Movie Night and Animation + Adult Animation need product-specific copy.
    tokens_a, tokens_b = _tokens(tag_a), _tokens(tag_b)
    if tokens_a and tokens_b and (tokens_a <= tokens_b or tokens_b <= tokens_a):
        longer, longer_facet, shorter = (
            (tag_b, facet_b, tag_a)
            if len(tokens_b) >= len(tokens_a)
            else (tag_a, facet_a, tag_b)
        )
        return (
            _standalone_collection(longer, longer_facet, media_type),
            longer,
            shorter,
        )

    values = _pair_values(tag_a, facet_a, tag_b, facet_b, "mood", "subgenre")
    if values:
        mood, genre = values
        return f"{mood} {_subgenre_collection(genre, media_type)}", mood, genre

    values = _pair_values(tag_a, facet_a, tag_b, facet_b, "origin", "subgenre")
    if values:
        origin, genre = values
        return f"{origin} {_subgenre_collection(genre, media_type)}", origin, genre

    values = _pair_values(tag_a, facet_a, tag_b, facet_b, "setting_period", "subgenre")
    if values:
        period, genre = values
        return (
            f"{_subgenre_collection(genre, media_type)} Set in "
            f"{period_phrase(period)}",
            period,
            genre,
        )

    values = _pair_values(tag_a, facet_a, tag_b, facet_b, "place", "subgenre")
    if values:
        place, genre = values
        return (
            f"{_subgenre_collection(genre, media_type)} Set in {place}",
            place,
            genre,
        )

    values = _pair_values(tag_a, facet_a, tag_b, facet_b, "theme", "subgenre")
    if values:
        theme, genre = values
        return (
            f"{_subgenre_collection(genre, media_type)} About {theme}",
            theme,
            genre,
        )

    values = _pair_values(tag_a, facet_a, tag_b, facet_b, "plot_device", "subgenre")
    if values:
        device, genre = values
        modifier = _PLOT_MODIFIERS.get(device, device)
        return f"{modifier} {_subgenre_collection(genre, media_type)}", device, genre

    values = _pair_values(tag_a, facet_a, tag_b, facet_b, "style", "subgenre")
    if values:
        style, genre = values
        modifier = _STYLE_MODIFIERS.get(style, style)
        return f"{modifier} {_subgenre_collection(genre, media_type)}", style, genre

    values = _pair_values(tag_a, facet_a, tag_b, facet_b, "pacing_format", "subgenre")
    if values:
        pacing, genre = values
        modifier = _PACING_MODIFIERS.get(pacing, pacing)
        return f"{modifier} {_subgenre_collection(genre, media_type)}", pacing, genre

    values = _pair_values(tag_a, facet_a, tag_b, facet_b, "release_decade", "subgenre")
    if values:
        decade, genre = values
        return (
            f"{_subgenre_collection(genre, media_type)} from the {decade}",
            decade,
            genre,
        )

    values = _pair_values(
        tag_a, facet_a, tag_b, facet_b, "source_franchise", "subgenre"
    )
    if values:
        source, genre = values
        collection = _subgenre_collection(genre, media_type)
        if source == "Based on a Book":
            name = f"{collection} Based on Books"
        elif source == "Based on a True Story":
            name = f"True-Story {collection}"
        elif source == "Comic Book Adaptation":
            name = f"Comic Book {collection}"
        elif source == "Manga Adaptation":
            name = f"Manga {collection}"
        elif source == "Sequel":
            name = f"{genre} Sequels"
        else:
            name = f"{collection} {_source_collection(source, media_type)}"
        return name, source, genre

    values = _pair_values(tag_a, facet_a, tag_b, facet_b, "seasonal", "subgenre")
    if values:
        seasonal, genre = values
        return (
            f"{seasonal} {_subgenre_collection(genre, media_type)}",
            seasonal,
            genre,
        )

    values = _pair_values(tag_a, facet_a, tag_b, facet_b, "audience", "subgenre")
    if values:
        audience, genre = values
        return (
            f"{_subgenre_collection(genre, media_type)} for {audience}",
            audience,
            genre,
        )

    values = _pair_values(tag_a, facet_a, tag_b, facet_b, "origin", "place")
    if values:
        origin, place = values
        return f"{origin} {media} Set in {place}", origin, place

    values = _pair_values(tag_a, facet_a, tag_b, facet_b, "release_decade", "origin")
    if values:
        decade, origin = values
        return f"{origin} {media} from the {decade}", decade, origin

    values = _pair_values(tag_a, facet_a, tag_b, facet_b, "release_decade", "style")
    if values:
        decade, style = values
        return (
            f"{_STYLE_MODIFIERS.get(style, style)} {media} from the {decade}",
            decade,
            style,
        )

    values = _pair_values(
        tag_a, facet_a, tag_b, facet_b, "release_decade", "source_franchise"
    )
    if values:
        decade, source = values
        return (
            f"{_source_collection(source, media_type)} from the {decade}",
            decade,
            source,
        )

    values = _pair_values(
        tag_a, facet_a, tag_b, facet_b, "release_decade", "plot_device"
    )
    if values:
        decade, device = values
        return f"{device} {media} from the {decade}", decade, device

    values = _pair_values(
        tag_a, facet_a, tag_b, facet_b, "release_decade", "pacing_format"
    )
    if values:
        decade, pacing = values
        modifier = _PACING_MODIFIERS.get(pacing, pacing)
        return f"{modifier} {media} from the {decade}", decade, pacing

    values = _pair_values(tag_a, facet_a, tag_b, facet_b, "origin", "pacing_format")
    if values:
        origin, pacing = values
        modifier = _PACING_MODIFIERS.get(pacing, pacing)
        return f"{modifier} {origin} {media}", origin, pacing

    values = _pair_values(tag_a, facet_a, tag_b, facet_b, "origin", "source_franchise")
    if values:
        origin, source = values
        return f"{origin} {_source_collection(source, media_type)}", origin, source

    values = _pair_values(tag_a, facet_a, tag_b, facet_b, "place", "theme")
    if values:
        place, theme = values
        place_modifier = "High-School" if place == "High School" else place
        return f"{place_modifier} {theme} {media}", place, theme

    return None


def name_for(
    tag_a: str,
    facet_a: str,
    tag_b: str,
    facet_b: str,
    media_type: str | None = None,
) -> tuple[str, str, str]:
    """Name a pair from its facets. Returns ``(name, ordered_a, ordered_b)``.

    Templates are directional -- ("mood", "subgenre") reads "Gritty Neo-Noirs",
    not "Neo-Noir Grittys" -- so the pair is reordered to match whichever
    direction has a template.

    Two corrections the first production preview forced, both visible in row
    titles users would have seen:

    * **Containment.** "Christmas" + "Christmas Movie" produced *Christmas
      Christmas Movies*. When one tag's words are a subset of the other's, the
      longer name alone is the row.
    * **Pluralisation.** A bare ``+ "s"`` produced *Documentarys* and
      *Mockumentarys*. :func:`app.taxonomy.pluralise` handles it.

    The bare "A B" fallback remains for diagnostic callers. Production scoring
    accepts only pairs handled by the editorial layer above, so fallback copy
    can never become a published shelf.
    """
    # Existing callers that omit media_type retain the legacy template output;
    # synthesis always supplies it and receives the polished product copy.
    if media_type:
        editorial = _editorial_name(tag_a, facet_a, tag_b, facet_b, media_type)
        if editorial:
            return editorial

    tokens_a, tokens_b = _tokens(tag_a), _tokens(tag_b)
    if tokens_a and tokens_b and (tokens_a <= tokens_b or tokens_b <= tokens_a):
        longer, longer_facet, shorter = (
            (tag_b, facet_b, tag_a)
            if len(tokens_b) >= len(tokens_a)
            else (tag_a, facet_a, tag_b)
        )
        return (
            _standalone_collection(longer, longer_facet, media_type),
            longer,
            shorter,
        )

    template = NAME_TEMPLATES.get((facet_a, facet_b))
    if template:
        return _render(template, tag_a, tag_b), tag_a, tag_b
    template = NAME_TEMPLATES.get((facet_b, facet_a))
    if template:
        return _render(template, tag_b, tag_a), tag_b, tag_a
    return f"{tag_a} {tag_b}", tag_a, tag_b


def _excluded_tags() -> set[str]:
    """Tag names that may not anchor a generated row (casefolded)."""
    raw = settings.synth_excluded_tags or ""
    return {p.strip().lower() for p in raw.split(",") if p.strip()}


def _excluded_words() -> set[str]:
    raw = settings.synth_excluded_words or ""
    return {p.strip().lower() for p in raw.split(",") if p.strip()}


def _name_is_allowed(name: str) -> bool:
    """False if a generated name contains an excluded word.

    A safety net behind the tag exclusion: a template or a future taxonomy
    addition could reintroduce a word even when the tag it came from is
    excluded. Matched on word boundaries so "Queer" is caught but a title
    containing it incidentally inside another word is not.
    """
    words = _tokens(name)
    return not (words & _excluded_words())


def _candidate_tags(
    db: Session, media_type: str
) -> tuple[dict[int, tuple[str, str, int]], int]:
    """Tags eligible to anchor a row, plus the corpus size.

    Returns ``{tag_id: (name, facet, doc_count)}``.
    """
    from workers.tagging_common import tagger_version

    version = tagger_version()
    eligible_ids = quality_floor_ids(db, media_type)
    common_filters = (
        MovieTag.media_type == media_type,
        MovieTag.tagger_version == version,
        MovieTag.weight_norm >= MIN_PAIR_WEIGHT_NORM,
        MovieTag.tmdb_id.in_(eligible_ids),
    )
    corpus = (
        db.query(func.count(func.distinct(MovieTag.tmdb_id)))
        .filter(*common_filters)
        .scalar()
        or 0
    )
    if corpus <= 0:
        return {}, 0

    floor = max(MIN_ROW_SUPPORT, int(corpus * MIN_DOC_FRACTION))
    ceiling = int(corpus * MAX_DOC_FRACTION)
    if ceiling <= floor:
        return {}, corpus

    rows = (
        db.query(
            Tag.id,
            Tag.name,
            Tag.category,
            func.count(func.distinct(MovieTag.tmdb_id)).label("doc_count"),
        )
        .join(MovieTag, MovieTag.tag_id == Tag.id)
        .filter(
            *common_filters,
            Tag.is_active.is_(True),
            Tag.display.is_(True),
        )
        .group_by(Tag.id, Tag.name, Tag.category)
        .having(func.count(func.distinct(MovieTag.tmdb_id)).between(floor, ceiling))
        .order_by(func.count(func.distinct(MovieTag.tmdb_id)).desc())
        .limit(MAX_CANDIDATE_TAGS)
        .all()
    )
    # Excluded tags are dropped here rather than after scoring, so they never
    # enter the pair join at all -- cheaper, and it frees their slots for
    # combinations that will actually ship.
    excluded = _excluded_tags()
    return {
        int(r[0]): (r[1], r[2], int(r[3])) for r in rows if r[1].lower() not in excluded
    }, corpus


def _pair_counts(
    db: Session, media_type: str, tag_ids: Sequence[int]
) -> list[tuple[int, int, int]]:
    """Co-occurrence counts for the candidate tags.

    ``a.tag_id < b.tag_id`` counts each unordered pair once, which halves the
    join and removes the (a,b)/(b,a) duplicate rows the greedy pass would
    otherwise have to dedupe.
    """
    if len(tag_ids) < 2:
        return []
    from workers.tagging_common import tagger_version

    version = tagger_version()
    a = aliased(MovieTag)
    b = aliased(MovieTag)
    rows = (
        db.query(a.tag_id, b.tag_id, func.count().label("pairs"))
        .select_from(a)
        .join(
            b,
            and_(
                b.tmdb_id == a.tmdb_id,
                b.media_type == a.media_type,
                b.tag_id > a.tag_id,
            ),
        )
        .filter(
            a.media_type == media_type,
            a.tagger_version == version,
            b.tagger_version == version,
            a.tmdb_id.in_(quality_floor_ids(db, media_type)),
            a.tag_id.in_(list(tag_ids)),
            b.tag_id.in_(list(tag_ids)),
            a.weight_norm >= MIN_PAIR_WEIGHT_NORM,
            b.weight_norm >= MIN_PAIR_WEIGHT_NORM,
        )
        .group_by(a.tag_id, b.tag_id)
        .having(func.count() >= MIN_PAIR_SUPPORT)
        .all()
    )
    return [(int(r[0]), int(r[1]), int(r[2])) for r in rows]


def _score_pairs(
    pairs: Iterable[tuple[int, int, int]],
    candidates: dict[int, tuple[str, str, int]],
    corpus: int,
    media_type: str,
) -> list[RowSpec]:
    """Turn raw co-occurrence counts into lift-ranked row specs."""
    specs: list[RowSpec] = []
    for tag_a, tag_b, support in pairs:
        info_a = candidates.get(tag_a)
        info_b = candidates.get(tag_b)
        if not info_a or not info_b:
            continue
        name_a, facet_a, count_a = info_a
        name_b, facet_b, count_b = info_b
        if facet_a == facet_b:
            # "Neo-Noir Slashers" is two subgenres arguing. Cross-facet pairs
            # are what read as a genre: a mood plus a subgenre, a place plus a
            # mood.
            continue
        if frozenset({name_a, name_b}) in _REDUNDANT_PAIRS:
            continue
        expected = (count_a / corpus) * (count_b / corpus) * corpus
        if expected <= 0:
            continue
        lift = support / expected
        if lift < MIN_LIFT:
            continue
        editorial = _editorial_name(name_a, facet_a, name_b, facet_b, media_type)
        if editorial is None:
            # Do not fill the row quota with a statistically correlated pair
            # that has no clear editorial relationship.  ``name_for`` keeps a
            # generic fallback for previews and callers, but production shelves
            # must pass this explicit product-language allowlist.
            continue
        name, ordered_a, ordered_b = editorial
        if not _name_is_allowed(name):
            continue
        ordered_facet_a = facet_a if ordered_a == name_a else facet_b
        ordered_facet_b = facet_b if ordered_b == name_b else facet_a
        specs.append(
            RowSpec(
                slug=_generated_slug(media_type, name),
                name=name,
                media_type=media_type,
                tag_a=ordered_a,
                tag_b=ordered_b,
                facet_a=ordered_facet_a,
                facet_b=ordered_facet_b,
                support=support,
                lift=round(lift, 3),
            )
        )
    return specs


def _select(specs: Sequence[RowSpec], limit: int) -> list[RowSpec]:
    """Greedy selection for diversity, highest lift first.

    Two constraints, both about the shelf a user actually sees: no tag anchors
    more than ``MAX_ROWS_PER_TAG`` rows, and each facet *pair* is used at most
    as often as it needs to be to stay varied. Pure lift ordering produces
    twenty variations on one dominant tag.
    """
    ordered = sorted(specs, key=lambda s: (-s.lift, -s.support, s.slug))
    per_tag: dict[str, int] = {}
    per_facet_pair: dict[tuple[str, str], int] = {}
    seen_slugs: set[str] = set()
    chosen: list[RowSpec] = []
    facet_cap = max(2, limit // 6)

    for spec in ordered:
        if len(chosen) >= limit:
            break
        if spec.slug in seen_slugs:
            continue
        if per_tag.get(spec.tag_a, 0) >= MAX_ROWS_PER_TAG:
            continue
        if per_tag.get(spec.tag_b, 0) >= MAX_ROWS_PER_TAG:
            continue
        # Exact editorial names do not need to reorder their source tags, so
        # count the facet pair as unordered.  Otherwise (origin, place) and
        # (place, origin) could each consume a full cap merely because their
        # taxonomy ids were inserted in a different order.
        facet_key = (
            min(spec.facet_a, spec.facet_b),
            max(spec.facet_a, spec.facet_b),
        )
        if per_facet_pair.get(facet_key, 0) >= facet_cap:
            continue
        chosen.append(spec)
        seen_slugs.add(spec.slug)
        per_tag[spec.tag_a] = per_tag.get(spec.tag_a, 0) + 1
        per_tag[spec.tag_b] = per_tag.get(spec.tag_b, 0) + 1
        per_facet_pair[facet_key] = per_facet_pair.get(facet_key, 0) + 1
    return chosen


def propose(db: Session, media_type: str, limit: int = 70) -> list[RowSpec]:
    """Rows this library's own tag distribution suggests. Reads only."""
    candidates, corpus = _candidate_tags(db, media_type)
    if len(candidates) < 2:
        logger.info(f"synth[{media_type}]: no candidate tags (corpus {corpus})")
        return []
    pairs = _pair_counts(db, media_type, list(candidates))
    specs = _score_pairs(pairs, candidates, corpus, media_type)
    chosen = _select(specs, limit)
    logger.info(
        f"synth[{media_type}]: {len(candidates)} candidates, {len(pairs)} pairs, "
        f"{len(specs)} above lift {MIN_LIFT}, {len(chosen)} selected"
    )
    return chosen


def apply(db: Session, specs: Sequence[RowSpec]) -> dict[str, int]:
    """Write the specs as ``source='generated'`` categories.

    Rows no longer proposed are **deactivated, not deleted** -- their
    ``universal_catalog_content`` rows are still serving until the next
    regeneration, and deleting the category cascades them away mid-flight.
    """
    existing = {
        c.id: c
        for c in db.query(UniversalCategory)
        .filter(UniversalCategory.source == "generated")
        .all()
    }
    stats = {"created": 0, "updated": 0, "deactivated": 0}
    keep: set[str] = set()

    for rank, spec in enumerate(specs):
        keep.add(spec.slug)
        # Generated rows are the universal product now. Curated compatibility
        # rows remain in the database, but do not reserve ordering or row
        # space.
        sort_order = rank + 1
        priority = max(0, 1000 - rank)
        row = existing.get(spec.slug)
        if row is None:
            db.add(
                UniversalCategory(
                    id=spec.slug,
                    name=spec.name,
                    description=(
                        f"{spec.tag_a} + {spec.tag_b} — "
                        f"{spec.support} titles, lift {spec.lift}"
                    ),
                    tier=6,
                    sort_order=sort_order,
                    media_type=spec.media_type,
                    tag_formula=spec.formula,
                    source="generated",
                    name_source="template",
                    name_spec_hash=spec.spec_hash,
                    manifest_priority=priority,
                    is_active=True,
                )
            )
            stats["created"] += 1
            continue

        row.name = spec.name
        row.tag_formula = spec.formula
        row.sort_order = sort_order
        row.manifest_priority = priority
        row.media_type = spec.media_type
        row.name_spec_hash = spec.spec_hash
        row.is_active = True
        stats["updated"] += 1

    for slug, row in existing.items():
        if slug not in keep and row.is_active:
            row.is_active = False
            stats["deactivated"] += 1

    db.commit()
    logger.info(
        f"synth: {stats['created']} created, {stats['updated']} updated, "
        f"{stats['deactivated']} deactivated"
    )
    return stats


def _interleave(by_type: dict[str, list[RowSpec]]) -> list[RowSpec]:
    """Alternate movie and TV specs, best-lift first within each.

    ``apply`` turns list position into ``manifest_priority``, and the home
    screen keeps the highest-priority rows within its row window. Emitting
    all 70 movie rows before all 70 TV rows therefore made every generated TV
    shelf unreachable: with 40 curated rows pinned above them the window is 20
    wide, so the first TV row needed the rotation to advance 51 positions --
    about a year at the default period.
    """
    out: list[RowSpec] = []
    lists = [by_type.get("movie", []), by_type.get("tv", [])]
    for index in range(max((len(lst) for lst in lists), default=0)):
        for lst in lists:
            if index < len(lst):
                out.append(lst[index])
    return out


def _dedupe_names(specs: Sequence[RowSpec]) -> list[RowSpec]:
    """Keep generated row names unique across movie and TV proposals.

    Curated rows no longer ship, so their names must not suppress a row the
    current corpus independently discovered.
    """
    taken: set[str] = set()
    kept: list[RowSpec] = []
    for spec in specs:
        key = spec.name.casefold()
        if key in taken:
            logger.debug(f"synthesis: dropping duplicate row name {spec.name!r}")
            continue
        taken.add(key)
        kept.append(spec)
    return kept


@dataclass(frozen=True)
class CompiledShelf:
    """A fully materialized and editorially approved shelf."""

    spec: RowSpec
    display_name: str
    ranked: tuple[tuple[int, float], ...]
    sample_titles: tuple[dict[str, Any], ...]
    editorial_score: int
    reason_codes: tuple[str, ...]
    decision_cached: bool
    top_title_quality: float


@dataclass(frozen=True)
class CompilationResult:
    shelves: tuple[CompiledShelf, ...]
    rejected: tuple[dict[str, Any], ...]
    proposed: int
    editorial: EditorialResult | None


def _word_root(value: str) -> str:
    """Small inflection fold for comparing product copy to canonical tags."""
    if len(value) > 4 and value.endswith("ies"):
        return value[:-3] + "y"
    if len(value) > 3 and value.endswith("s") and not value.endswith("ss"):
        return value[:-1]
    return value


def _concept_roots(value: str) -> set[str]:
    return {
        _word_root(token)
        for token in _tokens(value)
        if token not in _GENERIC_NAME_WORDS
    }


def _unsupported_name_concepts(name: str, spec: RowSpec) -> list[str]:
    """Canonical concepts added by an AI rename but absent from its formula.

    This intentionally validates semantic nouns rather than requiring the name
    to copy the template verbatim. Thus "Detective Case Files" is valid copy,
    while renaming ``CG Animation`` to "CG-Animated Adventure & Comedy" is not:
    Adventure and Comedy are real taxonomy concepts that do not drive the row.
    """
    display_roots = _concept_roots(name)
    allowed_roots = _concept_roots(
        f"{spec.name} {spec.tag_a} {spec.tag_b} {_media_label(spec.media_type)}"
    )
    unsupported: set[str] = set()
    for tag_name in BY_NAME:
        roots = _concept_roots(tag_name)
        if roots and roots <= display_roots and not roots <= allowed_roots:
            unsupported.add(tag_name)
    return sorted(unsupported)


def _valid_editorial_name(name: str, taken: set[str], spec: RowSpec) -> bool:
    normalized = " ".join(name.split())
    return (
        MIN_EDITORIAL_NAME_LENGTH <= len(normalized) <= MAX_EDITORIAL_NAME_LENGTH
        and normalized.casefold() not in taken
        and _name_is_allowed(normalized)
        and not _unsupported_name_concepts(normalized, spec)
    )


def _content_rating(item: MediaMetadata) -> str | None:
    for facts in (item.mdb_facts, item.tag_facts):
        if not isinstance(facts, dict):
            continue
        value = str(facts.get("certification") or "").strip()
        if value:
            return value
    return None


def _genre_names(item: MediaMetadata) -> list[str]:
    values = item.genres if isinstance(item.genres, list) else []
    names: list[str] = []
    for value in values:
        if isinstance(value, dict):
            name = str(value.get("name") or "").strip()
            if not name and value.get("id") == 99:
                name = "Documentary"
        elif value == 99 or str(value).strip() == "99":
            name = "Documentary"
        else:
            name = str(value or "").strip()
        if name and name not in names:
            names.append(name)
    return names


def _item_formats(item: MediaMetadata, assigned_tags: set[str]) -> set[str]:
    formats = set(assigned_tags & _FORMAT_TAGS)
    genres = {value.casefold() for value in _genre_names(item)}
    if "documentary" in genres:
        formats.add("Documentary")

    searchable = f"{item.title or ''} {item.overview or ''}".casefold()
    fact_terms: set[str] = set()
    for facts in (item.tag_facts, item.mdb_facts):
        if not isinstance(facts, dict):
            continue
        for key in ("keywords", "genres"):
            values = facts.get(key) or []
            if isinstance(values, (str, int, float)):
                values = [values]
            if not isinstance(values, list):
                continue
            for value in values:
                if isinstance(value, dict):
                    term = str(value.get("name") or value.get("title") or "")
                else:
                    term = str(value or "")
                if term.strip():
                    fact_terms.add(term.strip().casefold())
    documentary_markers = (
        "documentary",
        "documentaries",
        "docuseries",
        "docu-series",
        "non-fiction",
        "nonfiction",
        "making-of",
        "making of",
    )
    if any(marker in searchable for marker in documentary_markers) or any(
        marker in term for term in fact_terms for marker in documentary_markers
    ):
        formats.add("Documentary")
    if any(
        phrase in searchable
        for phrase in (
            "stand-up",
            "stand up special",
            "comedy special",
            "one-man show",
            "one man show",
        )
    ):
        formats.add("Stand-Up Special")
    if any(
        phrase in searchable
        for phrase in (
            "national theatre live",
            "filmed stage performance",
            "recorded live on stage",
        )
    ):
        formats.add("Live Performance")
    return formats


def _item_fits_shelf(
    spec: RowSpec,
    item: MediaMetadata | None,
    assigned_tags: set[str],
) -> bool:
    """Enforce format and audience promises before editorial review."""
    if item is None:
        # Catalog generation joins media_metadata; a missing value here is a
        # partial test fixture or a concurrently refreshed record, not evidence
        # that the title violates the shelf.
        return True

    required = {spec.tag_a, spec.tag_b}
    formats = _item_formats(item, assigned_tags)
    if "Documentary" in formats and "Documentary" not in required:
        return False
    if "Stand-Up Special" in formats and "Stand-Up Special" not in required:
        return False
    if "Live Performance" in formats and not required.intersection(_MUSIC_FORMAT_TAGS):
        return False
    if "Concert Film" in formats and not required.intersection(_MUSIC_FORMAT_TAGS):
        return False

    if required.intersection(_FAMILY_TAGS):
        certification = (_content_rating(item) or "").upper()
        if bool(item.adult) or certification in _MATURE_CERTIFICATIONS:
            return False
        if any(
            definition.maturity >= 3
            for tag in assigned_tags
            if (definition := BY_NAME.get(tag)) is not None
        ):
            return False
    return True


def _candidate_item_tags(
    db: Session, ranked_by_slug: dict[str, list[tuple[int, float]]]
) -> dict[tuple[str, int], set[str]]:
    ids = {tmdb_id for ranked in ranked_by_slug.values() for tmdb_id, _ in ranked}
    if not ids:
        return {}
    from workers.tagging_common import tagger_version

    by_item: dict[tuple[str, int], set[str]] = {}
    ordered_ids = sorted(ids)
    for start in range(0, len(ordered_ids), _LOOKUP_CHUNK_SIZE):
        rows = (
            db.query(MovieTag.media_type, MovieTag.tmdb_id, Tag.name)
            .join(Tag, Tag.id == MovieTag.tag_id)
            .filter(
                MovieTag.tagger_version == tagger_version(),
                MovieTag.tmdb_id.in_(ordered_ids[start : start + _LOOKUP_CHUNK_SIZE]),
                Tag.name.in_(_SHELF_SAFETY_TAGS),
            )
            .all()
        )
        for media_type, tmdb_id, tag_name in rows:
            by_item.setdefault((str(media_type), int(tmdb_id)), set()).add(
                str(tag_name)
            )
    return by_item


def _candidate_samples(
    db: Session, ranked_by_slug: dict[str, list[tuple[int, float]]]
) -> dict[tuple[str, int], MediaMetadata]:
    keys = {
        (spec_media_type, tmdb_id)
        for slug, ranked in ranked_by_slug.items()
        for spec_media_type in [slug.split("-", 2)[1]]
        for tmdb_id, _ in ranked
    }
    if not keys:
        return {}
    ordered_ids = sorted({tmdb_id for _, tmdb_id in keys})
    metadata: dict[tuple[str, int], MediaMetadata] = {}
    for start in range(0, len(ordered_ids), _LOOKUP_CHUNK_SIZE):
        rows = (
            db.query(MediaMetadata)
            .filter(
                MediaMetadata.tmdb_id.in_(
                    ordered_ids[start : start + _LOOKUP_CHUNK_SIZE]
                )
            )
            .all()
        )
        metadata.update(
            {
                (str(row.media_type), int(row.tmdb_id)): row
                for row in rows
                if (str(row.media_type), int(row.tmdb_id)) in keys
            }
        )
    return metadata


def compile_shelves(
    db: Session,
    limit_per_type: int = 70,
    *,
    use_ai: bool | None = None,
) -> CompilationResult:
    """Materialize, review, validate, rank, and deduplicate shelf candidates.

    This function never mutates the published category/content snapshot. The
    editor's reusable decision cache may be committed, but publication happens
    only in :func:`publish_compilation` after the complete pass succeeds.
    """
    proposal_limit = max(limit_per_type * 3, limit_per_type + 40)
    by_type = {
        media_type: propose(db, media_type, proposal_limit)
        for media_type in ("movie", "tv")
    }
    specs = _dedupe_names(_interleave(by_type))
    generator = CatalogGenerator(db)
    ranked_by_slug: dict[str, list[tuple[int, float]]] = {}
    rejected: list[dict[str, Any]] = []

    for spec in specs:
        transient = UniversalCategory(
            id=spec.slug,
            name=spec.name,
            media_type=spec.media_type,
            tag_formula=spec.formula,
        )
        ranked = generator.generate_universal_catalog(
            transient, limit=max(SYNTH_CANDIDATE_POOL, settings.catalog_size)
        )
        ranked_by_slug[spec.slug] = ranked

    # Clean the actual oversized membership before measuring density or showing
    # samples to the editor. This prevents one bad format assignment from
    # making a shelf look incoherent and ensures the published count is the
    # count that survived the product promise, not the raw SQL match count.
    metadata = _candidate_samples(db, ranked_by_slug)
    item_tags = _candidate_item_tags(db, ranked_by_slug)
    for spec in specs:
        ranked_by_slug[spec.slug] = [
            (tmdb_id, score)
            for tmdb_id, score in ranked_by_slug[spec.slug]
            if _item_fits_shelf(
                spec,
                metadata.get((spec.media_type, tmdb_id)),
                item_tags.get((spec.media_type, tmdb_id), set()),
            )
        ]
        ranked = ranked_by_slug[spec.slug]
        if len(ranked) < MIN_PUBLISHABLE_ITEMS:
            rejected.append(
                {
                    "id": spec.slug,
                    "name": spec.name,
                    "media_type": spec.media_type,
                    "actual_item_count": len(ranked),
                    "reason_codes": ["too_thin"],
                }
            )

    dense: list[dict[str, Any]] = []
    spec_by_slug = {spec.slug: spec for spec in specs}
    for spec in specs:
        ranked = ranked_by_slug[spec.slug]
        if len(ranked) < MIN_PUBLISHABLE_ITEMS:
            continue
        if {spec.tag_a, spec.tag_b}.intersection(_LOW_VALUE_SHELF_TAGS):
            rejected.append(
                {
                    "id": spec.slug,
                    "name": spec.name,
                    "media_type": spec.media_type,
                    "actual_item_count": len(ranked),
                    "reason_codes": ["low_tier_format"],
                }
            )
            continue
        samples: list[dict[str, Any]] = []
        familiarity_values: list[float] = []
        for tmdb_id, _ in ranked[:12]:
            item = metadata.get((spec.media_type, tmdb_id))
            if item is None:
                continue
            quality = round(_quality_value(item), 2)
            votes = int(item.vote_count or 0)
            familiarity = min(1.0, votes / 10_000.0)
            familiarity_values.append(quality + familiarity)
            assigned_tags = item_tags.get((spec.media_type, tmdb_id), set())
            samples.append(
                {
                    "tmdb_id": tmdb_id,
                    "title": item.title,
                    "year": (item.release_date or "")[:4],
                    "quality": quality,
                    "vote_count": votes,
                    "popularity": round(float(item.popularity or 0.0), 2),
                    "genres": _genre_names(item)[:6],
                    "content_rating": _content_rating(item),
                    "formats": sorted(_item_formats(item, assigned_tags)),
                    "adult": bool(item.adult),
                    "overview": " ".join((item.overview or "").split())[:240],
                }
            )
        dense.append(
            {
                "candidate_id": spec.slug,
                "spec_hash": spec.spec_hash,
                "deterministic_name": spec.name,
                "media_type": spec.media_type,
                "tags": [spec.tag_a, spec.tag_b],
                "facets": [spec.facet_a, spec.facet_b],
                "support": spec.support,
                "lift": spec.lift,
                "actual_item_count": len(ranked),
                "sample_titles": samples,
                "top_title_quality": round(
                    sum(familiarity_values) / max(1, len(familiarity_values)), 3
                ),
            }
        )

    ai_enabled = settings.synth_editor_enabled if use_ai is None else use_ai
    editorial: EditorialResult | None = None
    if ai_enabled and dense:
        editorial = review_candidates(db, dense)

    evaluated: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for candidate in dense:
        candidate_id = str(candidate["candidate_id"])
        if editorial is None:
            decision = {
                "decision": "keep",
                "display_name": candidate["deterministic_name"],
                "editorial_score": 50,
                "reason_codes": ["deterministic"],
                "cached": True,
            }
        else:
            decision = editorial.decisions[candidate_id]
        if decision["decision"] != "keep":
            rejected.append(
                {
                    **candidate,
                    "name": candidate["deterministic_name"],
                    "reason_codes": decision.get("reason_codes") or ["weak_concept"],
                    "editorial_score": decision["editorial_score"],
                    "cache_status": "cached" if decision.get("cached") else "new",
                }
            )
            continue
        if (
            editorial is not None
            and int(decision["editorial_score"]) < MIN_EDITORIAL_KEEP_SCORE
        ):
            rejected.append(
                {
                    **candidate,
                    "name": candidate["deterministic_name"],
                    "reason_codes": ["low_editorial_score"],
                    "editorial_score": decision["editorial_score"],
                    "cache_status": "cached" if decision.get("cached") else "new",
                }
            )
            continue
        evaluated.append((candidate, decision))

    # Editorial usefulness dominates. Density, recognizable high-quality
    # samples and support follow; rare-pair lift is deliberately last.
    evaluated.sort(
        key=lambda value: (
            -int(value[1]["editorial_score"]),
            -int(value[0]["actual_item_count"]),
            -float(value[0]["top_title_quality"]),
            -int(value[0]["support"]),
            -float(value[0]["lift"]),
            str(value[0]["candidate_id"]),
        )
    )

    accepted: list[CompiledShelf] = []
    taken_names: set[str] = set()
    per_type: dict[str, int] = {"movie": 0, "tv": 0}
    top_sets: dict[str, list[set[int]]] = {"movie": [], "tv": []}
    concept_keys: dict[str, set[str]] = {"movie": set(), "tv": set()}
    for candidate, decision in evaluated:
        spec = spec_by_slug[str(candidate["candidate_id"])]
        display_name = " ".join(str(decision.get("display_name") or "").split())
        unsupported = _unsupported_name_concepts(display_name, spec)
        if not _valid_editorial_name(display_name, taken_names, spec):
            rejected.append(
                {
                    **candidate,
                    "name": display_name,
                    "reason_codes": (
                        ["unsupported_name_concept"]
                        if unsupported
                        else ["invalid_name"]
                    ),
                    "unsupported_name_concepts": unsupported,
                    "editorial_score": decision["editorial_score"],
                }
            )
            continue
        if per_type[spec.media_type] >= limit_per_type:
            continue
        ranked = ranked_by_slug[spec.slug]
        top_ids = {tmdb_id for tmdb_id, _ in ranked[:VISIBLE_SHELF_ITEMS]}
        duplicate = any(
            len(top_ids & prior) / max(1, len(top_ids | prior)) > MAX_TOP40_JACCARD
            for prior in top_sets[spec.media_type]
        )
        if duplicate:
            rejected.append(
                {
                    **candidate,
                    "name": display_name,
                    "reason_codes": ["duplicate"],
                    "editorial_score": decision["editorial_score"],
                }
            )
            continue
        candidate_concepts = {
            tag
            for tag, facet in (
                (spec.tag_a, spec.facet_a),
                (spec.tag_b, spec.facet_b),
            )
            if facet in _CONCEPT_DEDUPE_FACETS
        }
        repeated_concepts = candidate_concepts & concept_keys[spec.media_type]
        if repeated_concepts:
            rejected.append(
                {
                    **candidate,
                    "name": display_name,
                    "reason_codes": ["duplicate_concept"],
                    "duplicate_concepts": sorted(repeated_concepts),
                    "editorial_score": decision["editorial_score"],
                }
            )
            continue
        taken_names.add(display_name.casefold())
        top_sets[spec.media_type].append(top_ids)
        concept_keys[spec.media_type].update(candidate_concepts)
        per_type[spec.media_type] += 1
        accepted.append(
            CompiledShelf(
                spec=spec,
                display_name=display_name,
                ranked=tuple(ranked),
                sample_titles=tuple(candidate["sample_titles"]),
                editorial_score=int(decision["editorial_score"]),
                reason_codes=tuple(decision.get("reason_codes") or []),
                decision_cached=bool(decision.get("cached")),
                top_title_quality=float(candidate["top_title_quality"]),
            )
        )

    return CompilationResult(
        shelves=tuple(accepted),
        rejected=tuple(rejected),
        proposed=len(specs),
        editorial=editorial,
    )


def publish_compilation(db: Session, compilation: CompilationResult) -> dict[str, Any]:
    """Atomically replace the generated shelf snapshot with one compilation."""
    if not compilation.shelves:
        raise RuntimeError(
            "No qualified synthesized shelves; current snapshot preserved"
        )
    existing = {
        row.id: row
        for row in db.query(UniversalCategory)
        .filter(UniversalCategory.source == "generated")
        .all()
    }
    keep: set[str] = set()
    stats: dict[str, Any] = {"created": 0, "updated": 0, "deactivated": 0}
    revision = utc_now()
    try:
        for rank, shelf in enumerate(compilation.shelves):
            spec = shelf.spec
            keep.add(spec.slug)
            row = existing.get(spec.slug)
            description = (
                f"{spec.tag_a} + {spec.tag_b} — {len(shelf.ranked)} titles; "
                f"editorial {shelf.editorial_score}; lift {spec.lift}"
            )
            if row is None:
                row = UniversalCategory(id=spec.slug, source="generated")
                db.add(row)
                stats["created"] += 1
            else:
                stats["updated"] += 1
            row.name = shelf.display_name
            row.description = description
            row.tier = 6
            row.sort_order = rank + 1
            row.media_type = spec.media_type
            row.tag_formula = spec.formula
            row.name_source = "ai" if compilation.editorial is not None else "template"
            row.name_spec_hash = spec.spec_hash
            row.manifest_priority = max(0, 1000 - rank)
            row.is_active = True
            row.updated_at = revision
            db.flush()
            db.query(UniversalCatalogContent).filter(
                UniversalCatalogContent.category_id == spec.slug
            ).delete(synchronize_session=False)
            db.bulk_insert_mappings(
                UniversalCatalogContent,
                [
                    {
                        "category_id": spec.slug,
                        "tmdb_id": tmdb_id,
                        "media_type": spec.media_type,
                        "rank": item_rank,
                        "match_score": score,
                        "last_updated": revision,
                    }
                    for item_rank, (tmdb_id, score) in enumerate(shelf.ranked, start=1)
                ],
            )

        for slug, row in existing.items():
            if slug not in keep and row.is_active:
                row.is_active = False
                stats["deactivated"] += 1
        from app.home_feed import invalidate_discovery

        invalidate_discovery(db)
        db.commit()
    except Exception:
        db.rollback()
        raise

    stats.update(
        {
            "published": len(compilation.shelves),
            "rejected": len(compilation.rejected),
            "proposed": compilation.proposed,
            "revision": revision.isoformat(),
            "editorial_cost_usd": (
                compilation.editorial.cost_usd if compilation.editorial else 0.0
            ),
            "editorial_reserved_cost_usd": (
                compilation.editorial.reserved_cost_usd
                if compilation.editorial
                else 0.0
            ),
            "editorial_cached": (
                compilation.editorial.cached if compilation.editorial else 0
            ),
            "editorial_reviewed": (
                compilation.editorial.reviewed if compilation.editorial else 0
            ),
        }
    )
    return stats


def rebuild_synthesized_shelves(
    db: Session, limit_per_type: int = 70
) -> dict[str, Any]:
    """The production staged synthesis entry point."""
    compilation = compile_shelves(db, limit_per_type)
    return publish_compilation(db, compilation)


def synthesize(db: Session, limit_per_type: int = 70) -> dict[str, Any]:
    """Propose and apply for both media types. The admin/worker entry point."""
    by_type = {
        media_type: propose(db, media_type, limit_per_type)
        for media_type in ("movie", "tv")
    }
    proposed = sum(len(v) for v in by_type.values())
    specs = _dedupe_names(_interleave(by_type))
    stats = apply(db, specs)
    stats["proposed"] = proposed
    stats["dropped_duplicate_names"] = proposed - len(specs)
    return stats


def preview(db: Session, limit_per_type: int = 70) -> list[dict[str, Any]]:
    """What ``synthesize`` would write, without writing it.

    Runs the same interleave and name-dedupe ``synthesize`` does, in the same
    order, so the preview is the plan rather than a differently-ordered
    approximation of it.
    """
    by_type = {
        media_type: propose(db, media_type, limit_per_type)
        for media_type in ("movie", "tv")
    }
    return [
        {
            "id": spec.slug,
            "name": spec.name,
            "media_type": spec.media_type,
            "tags": [spec.tag_a, spec.tag_b],
            "facets": [spec.facet_a, spec.facet_b],
            "support": spec.support,
            "lift": spec.lift,
        }
        for spec in _dedupe_names(_interleave(by_type))
    ]


def known_facet(name: str) -> str | None:
    """The taxonomy facet for a tag name, or None if it is off-vocabulary."""
    tag = BY_NAME.get(name)
    return tag.facet if tag else None
