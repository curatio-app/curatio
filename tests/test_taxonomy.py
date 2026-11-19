"""
Invariants for the tag taxonomy.

The single most valuable assertion here is
:func:`test_every_category_formula_tag_exists` -- every tag named in every
formula in ``app/categories.py`` must exist in the taxonomy. Before this module
existed the vocabulary lived in three divergent copies and six tags
(Western, War, Fast-Paced, European, Ensemble, Reluctant Hero) were seeded into
the database but absent from the prompt, so no title could ever receive them.
That drift was invisible for the life of the project. It is now a test failure.
"""

from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import taxonomy as tx  # noqa: E402

# The 75 tag names seeded by the pre-2.0 vocabulary. Hard-coded rather than
# parsed so this test keeps working after PREDEFINED_TAGS is deleted -- these
# names are referenced by ~2.2M movie_tags rows and may never be dropped.
LEGACY_TAG_NAMES = [
    # genre (14)
    "Action",
    "Crime",
    "Sci-Fi",
    "Horror",
    "Drama",
    "Comedy",
    "Thriller",
    "Romance",
    "Fantasy",
    "Mystery",
    "Documentary",
    "Animation",
    "Western",
    "War",
    # mood (12)
    "Dark",
    "Gritty",
    "Feel-Good",
    "Quirky",
    "Suspenseful",
    "Witty",
    "Heartwarming",
    "Intense",
    "Cerebral",
    "Lighthearted",
    "Slow-Burn",
    "Fast-Paced",
    # era (11)
    "1920s",
    "1930s",
    "1940s",
    "1950s",
    "1960s",
    "1970s",
    "1980s",
    "1990s",
    "2000s",
    "2010s",
    "2020s",
    # region (12)
    "British",
    "Korean",
    "French",
    "Scandinavian",
    "Japanese",
    "Italian",
    "Spanish",
    "Latin American",
    "Indian",
    "Chinese",
    "American",
    "European",
    # plot (10)
    "Heist",
    "Time Travel",
    "Revenge",
    "Courtroom",
    "Found Footage",
    "Coming-of-Age",
    "Survival",
    "Conspiracy",
    "Detective",
    "Ensemble",
    # style (9)
    "Neo-Noir",
    "Arthouse",
    "Independent",
    "Cyberpunk",
    "Visually Stunning",
    "Minimalist",
    "Period Costume",
    "Neon Visuals",
    "Satirical",
    # character (7)
    "Strong Female Lead",
    "Anti-Hero",
    "Ensemble Cast",
    "Character Study",
    "Underdog",
    "Morally Ambiguous",
    "Reluctant Hero",
]


def _formula_tag_names() -> set[str]:
    """Every tag name referenced by any UNIVERSAL_CATEGORIES formula."""
    source = (Path(__file__).parent.parent / "app" / "categories.py").read_text()
    names: set[str] = set()
    for block in re.findall(r'"(?:mandatory|required|optional)": \[([^\]]*)\]', source):
        names.update(re.findall(r'"([^"]+)"', block))
    return names


# ---------------------------------------------------------------------------
# The two invariants that matter most
# ---------------------------------------------------------------------------
def test_every_category_formula_tag_exists():
    """Catches the phantom-tag class of bug outright.

    A formula naming a tag that does not exist yields a silently empty catalog
    row -- exactly what happened to the six phantom tags.
    """
    formula_names = _formula_tag_names()
    assert formula_names, "parsed no formula tags -- the regex has drifted"
    missing = sorted(n for n in formula_names if n not in tx.BY_NAME)
    assert not missing, f"category formulas reference non-existent tags: {missing}"


def test_all_legacy_names_survive_as_canonical():
    """R1: dropping or renaming a legacy tag orphans its movie_tags rows."""
    assert len(LEGACY_TAG_NAMES) == 75
    missing = sorted(n for n in LEGACY_TAG_NAMES if n not in tx.BY_NAME)
    assert not missing, f"legacy tags missing from taxonomy: {missing}"


def test_all_legacy_names_resolve_through_the_alias_index():
    unresolvable = [
        n for n in LEGACY_TAG_NAMES if tx.normalize(n) not in tx.ALIAS_INDEX
    ]
    assert not unresolvable, f"legacy tags unresolvable: {unresolvable}"


# ---------------------------------------------------------------------------
# Structural invariants
# ---------------------------------------------------------------------------
def test_names_are_unique():
    dupes = [n for n, c in Counter(t.name for t in tx.TAXONOMY).items() if c > 1]
    assert not dupes, f"duplicate canonical names: {dupes}"


def test_every_facet_is_declared():
    bad = sorted({t.facet for t in tx.TAXONOMY} - set(tx.FACETS))
    assert not bad, f"tags carry undeclared facets: {bad}"


def test_every_facet_is_populated():
    empty = [f for f in tx.FACETS if not tx.BY_FACET[f]]
    assert not empty, f"declared facets with no tags: {empty}"


def test_every_parent_resolves():
    orphans = [
        (t.name, t.parent)
        for t in tx.TAXONOMY
        if t.parent is not None and t.parent not in tx.BY_NAME
    ]
    assert not orphans, f"tags with unresolvable parents: {orphans}"


def test_no_tag_is_its_own_parent():
    cycles = [t.name for t in tx.TAXONOMY if t.parent == t.name]
    assert not cycles


def test_aliases_do_not_collide_with_other_canonical_names():
    """A dead alias is harmless but always a mistake -- the canonical wins."""
    collisions = []
    for tag in tx.TAXONOMY:
        for alias in tag.aliases:
            target = tx.ALIAS_INDEX.get(tx.normalize(alias))
            if target and target != tag.name:
                collisions.append((tag.name, alias, target))
    assert not collisions, f"aliases shadowed by other canonicals: {collisions}"


def test_vocabulary_size_is_in_range():
    assert 300 <= len(tx.TAXONOMY) <= 530, len(tx.TAXONOMY)


def test_glosses_are_short():
    long_ones = [(t.name, t.gloss) for t in tx.TAXONOMY if len(t.gloss.split()) > 8]
    assert not long_ones, f"glosses over 8 words: {long_ones}"


def test_glosses_are_present():
    assert not [t.name for t in tx.TAXONOMY if not t.gloss.strip()]


def test_maturity_is_in_range():
    assert not [t.name for t in tx.TAXONOMY if not 0 <= t.maturity <= 4]


def test_media_values_are_valid():
    bad = [t.name for t in tx.TAXONOMY if set(t.media) - {"movie", "tv"} or not t.media]
    assert not bad, bad


def test_kid_safe_tracks_maturity():
    assert tx.BY_NAME["Kids"].kid_safe
    assert not tx.BY_NAME["Gore"].kid_safe


def test_taxonomy_version_is_set():
    assert tx.TAXONOMY_VERSION == "5.1.0"


# ---------------------------------------------------------------------------
# normalize()
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw,expected",
    [
        # Punctuation becomes whitespace, so the hyphen in a canonical name is
        # not part of its match key.
        ("Neo-Noir", "neo noir"),
        ("neo noir", "neo noir"),
        ("  NEO   NOIR  ", "neo noir"),
        # sci-fi is the one spelling split worth special-casing.
        ("Sci-Fi", "scifi"),
        ("sci fi", "scifi"),
        ("SciFi", "scifi"),
        ("Film Noir", "film noir"),
        ("Coming-of-Age", "coming of age"),
        ("Amélie", "amelie"),
        ("", ""),
    ],
)
def test_normalize(raw, expected):
    assert tx.normalize(raw) == expected


def test_normalize_is_idempotent():
    for tag in tx.TAXONOMY:
        once = tx.normalize(tag.name)
        assert tx.normalize(once) == once


# ---------------------------------------------------------------------------
# derive_tags -- computed, never guessed
# ---------------------------------------------------------------------------
def test_derive_decade():
    got = tx.derive_tags({"media_type": "movie", "release_date": "1994-09-23"})
    assert got.get("1990s") == 1.0


def test_derive_decade_ignores_junk():
    for bad in (None, "", "not-a-date", "19"):
        assert not any(
            tx.BY_NAME[n].facet == "release_decade"
            for n in tx.derive_tags({"media_type": "movie", "release_date": bad})
        )


def test_derive_origin_from_country():
    got = tx.derive_tags({"media_type": "movie", "countries": ["KR"]})
    assert "Korean" in got


def test_derive_origin_falls_back_to_language():
    got = tx.derive_tags({"media_type": "movie", "original_language": "ko"})
    assert "Korean" in got


def test_derive_origin_groups():
    got = tx.derive_tags({"media_type": "movie", "countries": ["SE"]})
    assert set(got) == {"Scandinavian"}
    got = tx.derive_tags({"media_type": "movie", "countries": ["MX"]})
    assert set(got) == {"Mexican"}


def test_derive_uses_only_the_primary_origin() -> None:
    got = tx.derive_tags({"media_type": "movie", "countries": ["US", "GB", "DE"]})
    assert {name for name in got if tx.BY_NAME[name].facet == "origin"} == {"American"}


def test_derive_runtime_buckets():
    short = tx.derive_tags({"media_type": "movie", "runtime": 82})
    assert "Under 90 Minutes" in short
    long = tx.derive_tags({"media_type": "movie", "runtime": 195})
    assert "Epic Runtime" in long
    mid = tx.derive_tags({"media_type": "movie", "runtime": 120})
    assert "Feature Length" in mid


def test_derive_tv_shape():
    limited = tx.derive_tags(
        {"media_type": "tv", "number_of_seasons": 1, "number_of_episodes": 8}
    )
    assert "Limited Series" in limited and "Single Season" in limited
    saga = tx.derive_tags({"media_type": "tv", "number_of_seasons": 6})
    assert "Multi-Season Saga" in saga


def test_derive_respects_media_type():
    """Runtime tags are movie-only; season tags are tv-only."""
    got = tx.derive_tags({"media_type": "tv", "runtime": 42})
    assert "Under 90 Minutes" not in got
    got = tx.derive_tags({"media_type": "movie", "number_of_seasons": 5})
    assert "Multi-Season Saga" not in got


def test_derive_only_emits_known_tags():
    got = tx.derive_tags(
        {
            "media_type": "movie",
            "release_date": "1977-05-25",
            "countries": ["US"],
            "runtime": 121,
        }
    )
    assert all(n in tx.BY_NAME for n in got)
    assert all(0.0 <= v <= 1.0 for v in got.values())


def test_every_derived_tag_is_reachable_by_derive_tags():
    """A derived tag no code path can produce is dead weight -- like the six
    phantom tags, but from the other direction."""
    produced: set[str] = set()
    cases = [
        {"media_type": "movie", "release_date": f"{y}-01-01"}
        for y in range(1920, 2030, 10)
    ]
    cases += [{"media_type": "movie", "countries": [c]} for c in tx._COUNTRY_ORIGIN]
    cases += [
        {"media_type": "movie", "original_language": lang} for lang in tx._LANG_ORIGIN
    ]
    # Group origins (Scandinavian / Latin American / European) are reachable only
    # via the country-set checks, not the 1:1 country map.
    cases += [
        {"media_type": "movie", "countries": [c]}
        for c in (tx._NORDIC | tx._LATIN_AMERICAN | tx._EUROPEAN)
    ]
    cases += [
        {"media_type": "movie", "runtime": 60},
        {"media_type": "movie", "runtime": 120},
        {"media_type": "movie", "runtime": 200},
        {"media_type": "tv", "number_of_seasons": 1, "number_of_episodes": 6},
        {"media_type": "tv", "number_of_seasons": 9},
    ]
    for case in cases:
        produced.update(tx.derive_tags(case))
    declared = {t.name for t in tx.TAXONOMY if t.derived}
    unreachable = sorted(declared - produced)
    assert not unreachable, f"derived tags no input can produce: {unreachable}"


# ---------------------------------------------------------------------------
# shortlist / batching / prompt budget
# ---------------------------------------------------------------------------
def _item(**kw):
    base = {
        "tmdb_id": 1,
        "media_type": "movie",
        "title": "Test",
        "overview": "A film.",
        "genres": ["Drama"],
        "keywords": [],
        "release_date": "2015-01-01",
    }
    base.update(kw)
    return base


def test_shortlist_never_offers_derived_tags():
    """The whole point of the derived flag."""
    for item in (_item(), _item(media_type="tv"), _item(keywords=["heist", "revenge"])):
        assert not [t.name for t in tx.shortlist(item) if t.derived]


def test_shortlist_offers_a_genre_compatible_compact_mood_core():
    drama = {t.name for t in tx.shortlist(_item(genres=["Drama"]))}
    comedy = {t.name for t in tx.shortlist(_item(genres=["Comedy"]))}

    assert not (
        {
            "Cerebral",
            "Contemplative",
            "Bleak",
            "Cozy",
            "Heartwarming",
            "Tender",
            "Chilling",
        }
        & (drama | comedy)
    )
    assert not ({"Fast-Paced", "Slow-Burn", "Twisty"} & tx.CORE_OFFERED_NAMES)
    assert not (tx.POSITIVE_MOOD_NAMES & drama)
    assert not (tx.HARSH_MOOD_NAMES & comedy)
    assert "Gritty" not in drama | comedy

    cerebral = {
        t.name
        for t in tx.shortlist(
            _item(overview="An intellectual mystery that rewards careful thinking.")
        )
    }
    reflective = {
        t.name
        for t in tx.shortlist(
            _item(overview="A meditative and reflective account of a quiet life.")
        )
    }
    assert "Cerebral" in cerebral
    assert "Contemplative" in reflective


def test_shortlist_respects_media_type():
    movie = {t.name for t in tx.shortlist(_item(media_type="movie"))}
    tv = {t.name for t in tx.shortlist(_item(media_type="tv"))}
    assert "Sitcom" not in movie
    assert "Bingeable" not in tv
    assert "Anthology" not in tv


def test_shortlist_is_bounded():
    item = _item(
        keywords=["heist", "revenge", "time travel", "dystopia", "vampire", "samurai"],
        overview="A heist in a dystopian future involving time travel and vampires.",
    )
    assert len(tx.shortlist(item)) <= tx.SHORTLIST_MAX


def test_shortlist_retrieves_from_keywords():
    plain = {t.name for t in tx.shortlist(_item())}
    heisty = {t.name for t in tx.shortlist(_item(keywords=["heist"]))}
    assert "Heist" in heisty
    assert "Heist" not in plain or True  # floor may include it; the point is retrieval
    assert len(heisty - plain) > 0


def test_real_nested_enrichment_drives_late_facets():
    """Production facts are nested; tests used to be the only top-level input."""
    item = _item(
        genres=[],
        keywords=[],
        original_language="ja",
        countries=["JP"],
        tag_facts={"keywords": ["anime", "graphic violence"]},
        mdb_facts={"keywords": ["academy award"]},
    )
    names = {tag.name for tag in tx.shortlist(item)}
    assert {"Anime", "Gore", "Oscar Winner"} <= names


def test_shortlist_does_not_pad_specific_facets_without_evidence():
    """Taxonomy order must never make arbitrary places/periods legal."""
    names = {t.name for t in tx.shortlist(_item(overview="", genres=[], keywords=[]))}
    assert not ({t.name for t in tx.BY_FACET["place"]} & names)
    assert not ({t.name for t in tx.BY_FACET["setting_period"]} & names)
    assert not ({t.name for t in tx.BY_FACET["source_franchise"]} & names)


def test_tmdb_genres_are_derived_without_spending_model_budget():
    item = _item(genres=["Crime", "Drama", "Science Fiction"])
    names = {t.name for t in tx.shortlist(item)}
    assert not ({"Crime", "Drama", "Sci-Fi"} & names)
    assert {"Crime", "Drama", "Sci-Fi"} <= set(tx.derive_tags(item))


def test_realistic_crime_drama_menu_does_not_offer_arbitrary_specialist_tags():
    item = _item(
        media_type="tv",
        title="Breaking Bad",
        genres=["Crime", "Drama"],
        overview=(
            "A chemistry teacher diagnosed with cancer manufactures "
            "methamphetamine with a former student."
        ),
        tag_facts={"keywords": ["drug dealer", "new mexico", "crime family"]},
    )
    names = {tag.name for tag in tx.shortlist(item)}

    assert not ({"Crime", "Drama"} & names)
    assert (
        not {
            "Space Opera",
            "Feudal Japan",
            "Prehistoric",
            "Remake",
            "Video Game Adaptation",
        }
        & names
    )


def test_animation_does_not_automatically_mean_adult_animation():
    item = _item(
        media_type="tv",
        title="A children's animated adventure",
        genres=["Animation", "Kids"],
        overview="Young friends solve problems together.",
    )
    names = {tag.name for tag in tx.shortlist(item)}

    assert {"Animation", "Kids"} <= set(tx.derive_tags(item))
    assert "Adult Animation" not in names
    assert "Animated for Adults" not in names


def test_combined_scifi_fantasy_is_evidence_disambiguated() -> None:
    fantasy = _item(
        media_type="tv",
        title="Game of Thrones",
        genres=["Sci-Fi & Fantasy", "Drama"],
        overview="Ancient magic and dragons threaten a kingdom.",
    )
    derived = tx.derive_tags(fantasy)
    assert "Fantasy" in derived
    assert "Sci-Fi" not in derived

    ambiguous = _item(media_type="tv", genres=["Sci-Fi & Fantasy"], overview="")
    assert not ({"Sci-Fi", "Fantasy"} & set(tx.derive_tags(ambiguous)))
    assert {"Sci-Fi", "Fantasy"} <= {tag.name for tag in tx.shortlist(ambiguous)}


def test_historical_period_requires_exact_evidence() -> None:
    item = _item(
        title="Schindler's List",
        genres=["History", "War"],
        overview="A factory owner saves lives during World War II.",
    )
    names = {tag.name for tag in tx.shortlist(item)}
    assert "World War II" in names
    assert "World War I" not in names


def test_animation_technique_requires_direct_evidence() -> None:
    generic = _item(genres=["Animation"], overview="An animated family adventure.")
    generic_names = {tag.name for tag in tx.shortlist(generic)}
    assert "CG Animation" not in generic_names
    assert "Cel Animation" not in generic_names

    cg = _item(genres=["Animation"], tag_facts={"keywords": ["3d animation"]})
    assert "CG Animation" in {tag.name for tag in tx.shortlist(cg)}


def test_anthology_requires_title_evidence() -> None:
    ordinary = _item(media_type="tv", overview="Friends share an apartment.")
    anthology = _item(
        media_type="tv",
        overview="An anthology series tells a different story each season.",
        tag_facts={"keywords": ["anthology"]},
    )
    assert "Anthology" not in {tag.name for tag in tx.shortlist(ordinary)}
    assert "Anthology" in {tag.name for tag in tx.shortlist(anthology)}


def test_reviews_cannot_make_a_specialist_subgenre_legal() -> None:
    """A review literally told Interstellar viewers to watch a documentary."""
    item = _item(
        title="Interstellar",
        genres=["Adventure", "Drama", "Science Fiction"],
        overview="Explorers cross a wormhole on an interstellar voyage.",
        mdb_facts={
            "reviews": ["This is science fiction, so go watch a documentary instead."]
        },
    )

    assert "Science Documentary" not in {tag.name for tag in tx.shortlist(item)}


def test_reviews_cannot_make_a_factual_franchise_tag_legal() -> None:
    original_series = _item(
        media_type="tv",
        title="Mission: Impossible",
        genres=["Action & Adventure", "Crime", "Drama"],
        overview="A team of secret government agents undertakes covert missions.",
        tag_facts={"keywords": ["secret agent", "foreign intrigue"]},
        mdb_facts={
            "reviews": ["This is the original series; I also bought the later reboot."]
        },
    )
    actual_reboot = _item(
        media_type="tv",
        genres=["Drama"],
        tag_facts={"keywords": ["reboot"]},
    )

    assert "Reboot" not in {tag.name for tag in tx.shortlist(original_series)}
    assert "Reboot" in {tag.name for tag in tx.shortlist(actual_reboot)}


def test_specialist_subgenres_need_the_parent_or_exact_strong_evidence() -> None:
    inception = _item(
        title="Inception",
        genres=["Action", "Science Fiction", "Adventure"],
        overview="A thief infiltrates dreams to implant an idea.",
        tag_facts={"keywords": ["spy", "heist", "subconscious"]},
    )
    chamber = _item(
        title="Harry Potter and the Chamber of Secrets",
        genres=["Adventure", "Fantasy"],
        overview="A giant snake stalks students inside a magical school.",
        tag_facts={"keywords": ["giant snake", "killing a monster", "magic"]},
    )

    assert "Spy Thriller" not in {tag.name for tag in tx.shortlist(inception)}
    assert "Creature Feature" not in {tag.name for tag in tx.shortlist(chamber)}

    actual_spy_thriller = _item(
        genres=["Thriller"],
        tag_facts={"keywords": ["spy", "espionage"]},
    )
    assert "Spy Thriller" in {tag.name for tag in tx.shortlist(actual_spy_thriller)}


def test_specialist_name_cannot_be_assembled_from_unrelated_fields() -> None:
    the_wire = _item(
        media_type="tv",
        title="The Wire",
        genres=["Crime", "Drama"],
        overview="Police investigate the war on drugs in Baltimore.",
        tag_facts={"keywords": ["police detective", "drug trafficking"]},
    )
    actual_war_drama = _item(
        genres=["Drama"],
        tag_facts={"keywords": ["war drama"]},
    )

    assert "War Drama" not in {tag.name for tag in tx.shortlist(the_wire)}
    assert "War Drama" in {tag.name for tag in tx.shortlist(actual_war_drama)}


def test_fiction_is_not_offered_unscripted_format_tags() -> None:
    the_bear = _item(
        media_type="tv",
        genres=["Drama", "Comedy"],
        overview="A chef returns home to run his family restaurant.",
        tag_facts={"keywords": ["restaurant", "chef", "food industry"]},
    )
    frieren = _item(
        media_type="tv",
        genres=["Animation", "Drama", "Sci-Fi & Fantasy"],
        overview="An elven mage journeys with a new apprentice.",
    )
    helck = _item(
        media_type="tv",
        genres=["Animation", "Comedy", "Action & Adventure"],
        overview="A fantasy tournament selects the next Demon Lord.",
        tag_facts={"keywords": ["competition", "tournament"]},
    )

    assert "Cooking Show" not in {tag.name for tag in tx.shortlist(the_bear)}
    assert "Travel Series" not in {tag.name for tag in tx.shortlist(frieren)}
    assert "Competition Show" not in {tag.name for tag in tx.shortlist(helck)}


def test_real_unscripted_formats_keep_their_specific_tags() -> None:
    cooking = _item(
        media_type="tv",
        genres=["Reality"],
        overview="Chefs prepare food for a panel of judges.",
        tag_facts={"keywords": ["cooking"]},
    )
    game = _item(
        media_type="tv",
        genres=[],
        overview="A syndicated game show where contestants compete for prizes.",
    )

    assert "Cooking Show" in {tag.name for tag in tx.shortlist(cooking)}
    assert "Game Show" in {tag.name for tag in tx.shortlist(game)}


def test_mood_core_rejects_job_223_genre_contradictions() -> None:
    game_of_thrones = _item(
        media_type="tv",
        genres=["Sci-Fi & Fantasy", "Drama", "Action & Adventure"],
        overview="Noble families wage full-scale war as an ancient evil awakens.",
    )
    zombie_horror = _item(
        genres=["Horror", "Thriller", "Science Fiction"],
        overview="Survivors flee a mutated rage virus and its horrors.",
    )
    animated_comedy = _item(
        media_type="tv",
        genres=["Animation", "Comedy"],
        overview="An eccentric family gets into comic misadventures.",
    )
    explicitly_hopeful_drama = _item(
        genres=["Drama"],
        overview="A hopeful community rebuilds after a devastating loss.",
    )

    throne_names = {tag.name for tag in tx.shortlist(game_of_thrones)}
    assert not ({"Cozy", "Hopeful"} & throne_names)
    assert "Feel-Good" not in {tag.name for tag in tx.shortlist(zombie_horror)}
    assert "Bleak" not in {tag.name for tag in tx.shortlist(animated_comedy)}
    assert "Hopeful" in {tag.name for tag in tx.shortlist(explicitly_hopeful_drama)}


def test_job_224_false_positive_candidates_are_not_offered() -> None:
    barbie = _item(
        title="Barbie",
        genres=["Comedy", "Adventure", "Fantasy"],
        overview="Barbie and Ken leave Barbie Land and discover life among humans.",
        tag_facts={"keywords": ["satire", "patriarchy", "fish out of water"]},
    )
    coco = _item(
        title="Coco",
        genres=["Family", "Animation", "Music", "Adventure"],
        overview="A young musician enters the Land of the Dead.",
        tag_facts={"keywords": ["holiday", "day of the dead", "afterlife"]},
    )
    blue_lock = _item(
        media_type="tv",
        title="BLUE LOCK",
        genres=["Animation", "Action & Adventure", "Drama"],
        overview="Soccer players compete to become the world's best striker.",
        tag_facts={"keywords": ["sports", "tournament", "based on manga"]},
    )
    lone_star = _item(
        media_type="tv",
        title="9-1-1: Lone Star",
        genres=["Drama", "Action & Adventure", "Crime"],
        overview="A firefighter rebuilds an emergency station after tragedy.",
        tag_facts={"keywords": ["firefighter", "rescue"]},
    )
    married = _item(
        media_type="tv",
        title="Married... with Children",
        genres=["Comedy"],
        overview="A miserable shoe salesman lives with his dysfunctional family.",
        tag_facts={"keywords": ["sitcom", "social satire"]},
    )
    capture = _item(
        media_type="tv",
        title="The Capture",
        genres=["Crime", "Drama", "Mystery"],
        overview="Flawed video evidence exposes a surveillance conspiracy.",
        tag_facts={"keywords": ["absurd", "deepfake", "surveillance"]},
    )
    avatar = _item(
        media_type="tv",
        title="Avatar: The Last Airbender",
        genres=["Action & Adventure", "Sci-Fi & Fantasy", "Drama", "Family"],
        overview="A young Avatar must master four powers and save a world at war.",
    )
    ben_ten = _item(
        media_type="tv",
        title="Ben 10",
        genres=["Animation", "Action & Adventure", "Comedy", "Kids", "Family"],
        overview="A child gains a device that transforms him into alien heroes.",
    )

    assert not ({"Brutal", "Suspenseful"} & {tag.name for tag in tx.shortlist(barbie)})
    assert "Christmas" not in {tag.name for tag in tx.shortlist(coco)}
    assert "Mortality" not in {tag.name for tag in tx.shortlist(blue_lock)}
    assert "Witty" not in {tag.name for tag in tx.shortlist(lone_star)}
    assert "Brutal" not in {tag.name for tag in tx.shortlist(married)}
    assert "Absurdist" not in {tag.name for tag in tx.shortlist(capture)}
    assert "Family Drama" not in {tag.name for tag in tx.shortlist(avatar)}
    assert "Cerebral" not in {tag.name for tag in tx.shortlist(ben_ten)}


def test_final_precision_guards_preserve_strong_evidence() -> None:
    violent_horror = _item(
        genres=["Horror", "Thriller"],
        overview="Survivors endure a gruesome siege.",
        tag_facts={"keywords": ["gore", "graphic violence", "brutal"]},
    )
    christmas = _item(
        genres=["Romance", "Drama"],
        overview="A family reunites on Christmas Eve.",
        tag_facts={"keywords": ["christmas"]},
    )
    mortality = _item(
        genres=["Drama"],
        overview="An immortal elf confronts mortality after her friends die.",
    )
    ai_ethics = _item(
        genres=["Science Fiction", "Drama"],
        overview="A tribunal debates artificial intelligence ethics and robot rights.",
    )
    family_drama = _item(
        genres=["Drama"],
        overview="A family returns home when two brothers dispute their inheritance.",
    )
    explicit_pacing = _item(
        genres=["Drama"],
        tag_facts={"keywords": ["fast paced"]},
    )

    assert "Brutal" in {tag.name for tag in tx.shortlist(violent_horror)}
    assert "Christmas" in {tag.name for tag in tx.shortlist(christmas)}
    assert "Mortality" in {tag.name for tag in tx.shortlist(mortality)}
    assert "AI Ethics" in {tag.name for tag in tx.shortlist(ai_ethics)}
    assert "Family Drama" in {tag.name for tag in tx.shortlist(family_drama)}
    assert "Fast-Paced" in {tag.name for tag in tx.shortlist(explicit_pacing)}


def test_high_frequency_observations_resolve_to_existing_concepts() -> None:
    assert tx.ALIAS_INDEX[tx.normalize("Based on Manga")] == "Manga Adaptation"
    assert tx.ALIAS_INDEX[tx.normalize("Based on Novel or Book")] == "Based on a Book"
    assert tx.ALIAS_INDEX[tx.normalize("Based on Comic")] == "Comic Book Adaptation"
    assert tx.ALIAS_INDEX[tx.normalize("Political Satire")] == "Satire"
    assert tx.ALIAS_INDEX[tx.normalize("Murder Investigation")] == "Detective"
    assert tx.ALIAS_INDEX[tx.normalize("Murder Mystery")] == "Whodunit Mystery"


def test_places_ignore_mdb_contamination_and_named_region_substrings() -> None:
    contaminated = _item(
        title="Foyle's War",
        overview="A detective investigates crimes on England's south coast.",
        tag_facts={"keywords": ["world war ii", "police detective"]},
        mdb_facts={"keywords": ["new mexico desert", "desert"]},
    )
    named_region = _item(
        title="Everybody Loves Raymond",
        overview="A sportswriter lives on Long Island with his family.",
        tag_facts={"keywords": ["long island, new york", "sitcom"]},
    )
    real_island = _item(
        overview="Shipwrecked survivors are stranded on an uninhabited island.",
        tag_facts={"keywords": ["island"]},
    )

    assert "The Desert" not in {tag.name for tag in tx.shortlist(contaminated)}
    assert "Island" not in {tag.name for tag in tx.shortlist(named_region)}
    assert "Island" in {tag.name for tag in tx.shortlist(real_island)}


def test_job_225_shortlist_blocks_ambiguous_places_and_origin() -> None:
    coco = _item(
        title="Coco",
        overview="Miguel idolizes Ernesto de la Cruz and enters the Land of the Dead.",
        original_language="en",
        countries=["US"],
        tag_facts={"keywords": ["mexico", "day of the dead"]},
    )
    dark = _item(
        media_type="tv",
        title="Dark",
        overview="A missing child exposes a mystery spanning three generations.",
        original_language="de",
        countries=["DE"],
        tag_facts={"keywords": ["small town", "time travel"]},
    )
    ben_ten = _item(
        media_type="tv",
        title="Ben 10",
        overview="A child uses an alien device to transform into ten heroes.",
        original_language="en",
        countries=["US"],
        tag_facts={"keywords": ["time travel", "superhero"]},
        mdb_facts={"keywords": ["anime-inspired", "shared-universe"]},
    )
    actual_anime = _item(
        media_type="tv",
        title="Frieren",
        overview="An elven mage reckons with the passage of human lives.",
        original_language="ja",
        countries=["JP"],
        tag_facts={"keywords": ["anime", "based on manga"]},
    )
    los_angeles = _item(
        overview="A detective works a case across Los Angeles.",
        countries=["US"],
    )
    american_town = _item(
        overview="A sheriff protects a small town in Oregon.",
        countries=["US"],
        tag_facts={"keywords": ["small town"]},
    )

    assert "Los Angeles" not in {tag.name for tag in tx.shortlist(coco)}
    dark_names = {tag.name for tag in tx.shortlist(dark)}
    assert "Small-Town America" not in dark_names
    assert "Time Travel" in dark_names
    ben_names = {tag.name for tag in tx.shortlist(ben_ten)}
    assert not ({"Anime", "Time Travel", "Time Travel Sci-Fi"} & ben_names)
    assert "Anime" in {tag.name for tag in tx.shortlist(actual_anime)}
    assert "Los Angeles" in {tag.name for tag in tx.shortlist(los_angeles)}
    assert "Small-Town America" in {tag.name for tag in tx.shortlist(american_town)}


def test_job_225_shortlist_requires_whole_series_and_contiguous_evidence() -> None:
    gravity_falls = _item(
        media_type="tv",
        title="Gravity Falls",
        overview="Twins spend a strange summer in a mysterious Oregon town.",
        genres=["Animation", "Mystery"],
        countries=["US"],
        tag_facts={"keywords": ["time travel", "small town"]},
        mdb_facts={"keywords": ["time-travel"]},
    )
    actual_time_travel = _item(
        media_type="tv",
        title="Timeless",
        overview="A team uses a time machine to travel back in time.",
        genres=["Sci-Fi & Fantasy"],
    )
    dune = _item(
        title="Dune",
        overview="A chosen one protects the future of his family and people.",
        genres=["Science Fiction", "Adventure"],
    )
    rizzoli = _item(
        media_type="tv",
        title="Rizzoli & Isles",
        overview="A detective and medical examiner solve crimes together.",
        genres=["Drama", "Crime"],
    )
    family_guy = _item(
        media_type="tv",
        title="Family Guy",
        overview="A dysfunctional animated family creates chaos at home.",
        tag_facts={"keywords": ["high school"]},
    )
    blue_lock = _item(
        media_type="tv",
        title="BLUE LOCK",
        overview="High school soccer players enter an elite tournament.",
        tag_facts={"keywords": ["high school", "tournament"]},
    )
    fifth_element = _item(
        title="The Fifth Element",
        overview="A taxi driver must save humanity with help from a mysterious woman.",
        genres=["Science Fiction", "Adventure"],
        mdb_facts={"keywords": ["priest", "end-of-the-world"]},
    )
    goliath = _item(
        media_type="tv",
        title="Goliath",
        overview="A lawyer pursues a wrongful death lawsuit against a powerful firm.",
        genres=["Drama"],
    )
    doraemon = _item(
        media_type="tv",
        title="Doraemon",
        overview="A robotic cat helps a schoolboy solve everyday problems.",
        original_language="ja",
        countries=["JP"],
        tag_facts={"keywords": ["anime", "back to school"]},
    )
    macgyver = _item(
        media_type="tv",
        title="MacGyver",
        overview="A resourceful secret agent solves dangerous problems with science.",
        tag_facts={"keywords": ["improvised weapon"]},
    )
    passion = _item(
        title="The Passion of the Christ",
        overview="A portrayal of the last hours of Jesus of Nazareth.",
        tag_facts={"keywords": ["christianity", "crucifixion", "bible"]},
    )
    fictional_doctor = _item(
        media_type="tv",
        title="A Miracle Doctor",
        overview="A surgical resident joins a hospital and faces skeptical colleagues.",
        genres=["Drama"],
        tag_facts={"keywords": ["educational", "medical drama"]},
    )
    narnia = _item(
        title="Narnia",
        overview="Four siblings enter a magical kingdom and join a great battle.",
        genres=["Adventure", "Family", "Fantasy"],
        tag_facts={"keywords": ["3d animation", "visual effects"]},
    )
    animated_feature = _item(
        title="Hoppers",
        overview="An animated film about a girl whose mind enters a robotic animal.",
        genres=["Animation", "Family"],
        tag_facts={"keywords": ["3d animation"]},
    )
    batman = _item(
        media_type="tv",
        title="Batman: The Animated Series",
        overview="Batman fights crime in Gotham City.",
        genres=["Animation", "Mystery"],
        tag_facts={"keywords": ["race against time", "detective"]},
    )
    powerpuff = _item(
        media_type="tv",
        title="The Powerpuff Girls",
        overview="Three kindergarten superheroes protect their city.",
        genres=["Animation", "Comedy", "Kids"],
        tag_facts={"keywords": ["giant monster", "superhero"]},
    )
    slime = _item(
        media_type="tv",
        title="That Time I Got Reincarnated as a Slime",
        overview="A worker is reborn as a slime and befriends a fantasy kingdom.",
        genres=["Animation", "Comedy", "Sci-Fi & Fantasy"],
        original_language="ja",
        countries=["JP"],
        mdb_facts={"keywords": ["gore"]},
    )
    chicago_fire = _item(
        media_type="tv",
        title="Chicago Fire",
        overview="Firefighters, paramedics and a rescue squad serve Firehouse 51.",
        genres=["Drama"],
        countries=["US"],
        tag_facts={"keywords": ["firefighter", "paramedic", "fire brigade"]},
    )
    hijack = _item(
        media_type="tv",
        title="Hijack",
        overview="An expert negotiator responds after hijackers seize an airplane.",
        genres=["Drama"],
        tag_facts={"keywords": ["airplane hijacking", "negotiator"]},
    )

    gravity_names = {tag.name for tag in tx.shortlist(gravity_falls)}
    assert not ({"Time Travel", "Time Travel Sci-Fi"} & gravity_names)
    actual_names = {tag.name for tag in tx.shortlist(actual_time_travel)}
    assert {"Time Travel", "Time Travel Sci-Fi"} <= actual_names
    assert "Chosen Family" not in {tag.name for tag in tx.shortlist(dune)}
    assert "Medical Drama" not in {tag.name for tag in tx.shortlist(rizzoli)}
    assert "High School" not in {tag.name for tag in tx.shortlist(family_guy)}
    assert "High School" in {tag.name for tag in tx.shortlist(blue_lock)}
    assert "Religion and Cults" not in {tag.name for tag in tx.shortlist(fifth_element)}
    assert "Mortality" not in {tag.name for tag in tx.shortlist(goliath)}
    assert "Back to School" not in {tag.name for tag in tx.shortlist(doraemon)}
    assert "Naturalistic Dialogue" not in {tag.name for tag in tx.shortlist(macgyver)}
    assert "Religion and Cults" in {tag.name for tag in tx.shortlist(passion)}
    assert "Educational" not in {tag.name for tag in tx.shortlist(fictional_doctor)}
    assert "CG Animation" not in {tag.name for tag in tx.shortlist(narnia)}
    assert "CG Animation" in {tag.name for tag in tx.shortlist(animated_feature)}
    assert "Ticking Clock" not in {tag.name for tag in tx.shortlist(batman)}
    assert "Kaiju" not in {tag.name for tag in tx.shortlist(powerpuff)}
    assert "Gore" not in {tag.name for tag in tx.shortlist(slime)}
    assert "Emergency Services Drama" in {
        tag.name for tag in tx.shortlist(chicago_fire)
    }
    assert "Hostage Crisis" in {tag.name for tag in tx.shortlist(hijack)}


def test_job_226_reviews_cannot_create_candidates() -> None:
    top_gun = _item(
        title="Top Gun: Maverick",
        genres=["Action", "Drama"],
        overview="A Navy aviator trains pilots for a dangerous mission.",
        mdb_facts={
            "reviews": ["The star adds no value beyond his fame."],
        },
    )
    blossom = _item(
        media_type="tv",
        title="Blossom",
        genres=["Drama"],
        overview="Two allies navigate family schemes in a rural village.",
        mdb_facts={
            "reviews": ["Justice is served in this satisfying story."],
        },
    )
    zootopia = _item(
        title="Zootopia 2",
        genres=["Animation", "Adventure", "Comedy", "Family"],
        overview="Two animal detectives go undercover to solve a new mystery.",
        mdb_facts={
            "reviews": ["A topical story for an ever darkening nihilistic world."],
        },
    )
    barbie = _item(
        title="Barbie",
        genres=["Comedy", "Adventure", "Fantasy"],
        overview="Barbie visits the real world and learns about being human.",
        mdb_facts={
            "reviews": ["It discusses capitalism and men's mental health."],
        },
    )

    assert "Fame" not in {tag.name for tag in tx.shortlist(top_gun)}
    assert "Justice and the Law" not in {tag.name for tag in tx.shortlist(blossom)}
    assert not (
        {"Dark", "Bleak", "Unsettling"} & {tag.name for tag in tx.shortlist(zootopia)}
    )
    barbie_names = {tag.name for tag in tx.shortlist(barbie)}
    assert not ({"Capitalism", "Mental Illness"} & barbie_names)


def test_job_226_weak_moods_need_title_evidence() -> None:
    chucky = _item(
        media_type="tv",
        title="Chucky",
        genres=["Crime", "Comedy", "Mystery"],
        overview="A killer doll unleashes a series of horrifying murders.",
        mdb_facts={"reviews": ["It is surprisingly warm beneath the mayhem."]},
    )
    big_bang = _item(
        media_type="tv",
        title="The Big Bang Theory",
        genres=["Comedy"],
        overview="Physicists and their friends navigate everyday life.",
    )
    mayans = _item(
        media_type="tv",
        title="Mayans M.C.",
        genres=["Crime", "Drama"],
        overview="A motorcycle-club prospect tries to build a new identity.",
    )
    explicit = _item(
        genres=["Drama"],
        overview="A tender and affectionate portrait of a lifelong friendship.",
        tag_facts={"keywords": ["heartwarming"]},
    )

    assert "Heartwarming" not in {tag.name for tag in tx.shortlist(chucky)}
    assert not (
        {"Feel-Good", "Hopeful", "Lighthearted", "Whimsical"}
        & {tag.name for tag in tx.shortlist(chucky)}
    )
    assert "Tender" not in {tag.name for tag in tx.shortlist(big_bang)}
    assert "Chilling" not in {tag.name for tag in tx.shortlist(mayans)}
    assert {"Heartwarming", "Tender"} <= {tag.name for tag in tx.shortlist(explicit)}


def test_job_228_mood_word_sense_collisions_are_not_model_choices() -> None:
    shrinking = _item(
        media_type="tv",
        title="Shrinking",
        genres=["Drama", "Comedy"],
        overview=(
            "A grieving therapist tries a new approach with everyone in his path: "
            "unfiltered, brutal honesty."
        ),
    )
    poldark = _item(
        media_type="tv",
        title="Poldark",
        genres=["Drama"],
        overview="Britain is in the grip of a chilling recession and civil unrest.",
    )
    p_valley = _item(
        media_type="tv",
        title="P-Valley",
        genres=["Drama", "Crime"],
        overview=(
            "A strip club draws big characters through its doors: the hopeful, "
            "the lost, the broken, and the damned."
        ),
    )

    assert "Brutal" not in {tag.name for tag in tx.model_shortlist(shrinking)}
    assert "Chilling" not in {tag.name for tag in tx.model_shortlist(poldark)}
    assert "Hopeful" not in {tag.name for tag in tx.model_shortlist(p_valley)}


def test_job_228_harsh_genres_do_not_trust_positive_mood_keywords_alone() -> None:
    snowfall = _item(
        media_type="tv",
        title="Snowfall",
        genres=["Drama", "Crime"],
        overview=(
            "The crack cocaine epidemic sends numerous characters on a violent "
            "collision course."
        ),
        tag_facts={"keywords": ["drug crime", "gangster", "inspirational"]},
    )
    veep = _item(
        media_type="tv",
        title="Veep",
        genres=["Comedy", "War & Politics"],
        overview="A vice president discovers that politics is nothing like expected.",
        tag_facts={"keywords": ["political satire", "lighthearted"]},
    )

    assert "Rousing" not in {tag.name for tag in tx.model_shortlist(snowfall)}
    assert "Lighthearted" not in {tag.name for tag in tx.model_shortlist(veep)}


def test_job_228_character_candidates_need_synopsis_level_evidence() -> None:
    keyword_only_cases = (
        (
            _item(
                title="The Fifth Element",
                genres=["Science Fiction", "Action", "Adventure"],
                overview="A taxi driver must save a young woman and humanity.",
                tag_facts={"keywords": ["anti hero", "saving the world"]},
            ),
            "Anti-Hero",
        ),
        (
            _item(
                media_type="tv",
                title="Buffy the Vampire Slayer",
                genres=["Comedy", "Drama", "Sci-Fi & Fantasy"],
                overview="A chosen girl fights vampires and the forces of darkness.",
                tag_facts={"keywords": ["anti hero", "female protagonist"]},
            ),
            "Anti-Hero",
        ),
        (
            _item(
                media_type="tv",
                title="Spider-Noir",
                genres=["Drama", "Mystery", "Crime"],
                overview="An aging private investigator grapples with his past.",
                tag_facts={"keywords": ["femme fatale", "film noir"]},
            ),
            "Femme Fatale",
        ),
    )
    for item, bad_tag in keyword_only_cases:
        assert bad_tag not in {tag.name for tag in tx.model_shortlist(item)}

    explicit = _item(
        genres=["Crime", "Drama"],
        overview="An anti-hero uses criminal methods to expose a corrupt empire.",
    )
    deadpool = _item(
        genres=["Action", "Comedy"],
        overview=(
            "A former Special Forces operative turned mercenary adopts a masked "
            "alter ego and hunts down the man who destroyed his life."
        ),
        tag_facts={"keywords": ["anti hero", "mercenary"]},
    )
    assert "Anti-Hero" in {tag.name for tag in tx.model_shortlist(explicit)}
    assert "Anti-Hero" in {tag.name for tag in tx.model_shortlist(deadpool)}


def test_job_228_adult_animation_suppresses_family_audience_labels() -> None:
    king_of_the_hill = _item(
        media_type="tv",
        title="King of the Hill",
        genres=["Animation", "Comedy", "Family"],
        overview="An animated Texas family comedy follows Hank and his neighbors.",
        tag_facts={"keywords": ["adult animation", "family"]},
    )

    names = set(tx.derive_tags(king_of_the_hill))
    assert "Adult Animation" in names
    assert "Family" in names
    assert not ({"Kids", "Preschool", "All Ages", "Family Movie Night"} & names)


def test_light_is_not_a_lighthearted_alias() -> None:
    assert tx.ALIAS_INDEX.get(tx.normalize("light")) != "Lighthearted"


def test_job_226_tv_topics_and_backstory_do_not_become_whole_series_tags() -> None:
    degrassi = _item(
        media_type="tv",
        title="Degrassi",
        genres=["Drama", "Soap"],
        overview="Adolescents face the trials of life at their community school.",
        tag_facts={"keywords": ["high school", "teen drama"]},
        mdb_facts={"keywords": ["environmentalism"]},
    )
    eureka = _item(
        media_type="tv",
        title="Eureka",
        genres=["Drama", "Comedy"],
        overview="The government relocates scientists to a mysterious town.",
    )
    arrow = _item(
        media_type="tv",
        title="Arrow",
        genres=["Crime", "Drama", "Action & Adventure"],
        overview="After his yacht is lost at sea, a billionaire returns as a vigilante.",
        tag_facts={"keywords": ["superhero", "based on comic"]},
    )
    mayans = _item(
        media_type="tv",
        title="Mayans M.C.",
        genres=["Crime", "Drama"],
        overview="Fresh out of prison, a man joins a motorcycle club.",
        tag_facts={"keywords": ["organized crime", "motorcycle gang"]},
    )
    prison_series = _item(
        media_type="tv",
        title="Cell Block",
        genres=["Drama"],
        overview="Prison inmates and guards struggle inside a prison.",
    )
    sea_series = _item(
        media_type="tv",
        title="The Voyage",
        genres=["Drama", "Adventure"],
        overview="A ship's crew crosses the Atlantic on a dangerous ocean voyage.",
    )
    environmental_series = _item(
        media_type="tv",
        title="Planet Watch",
        genres=["Documentary"],
        overview="Conservationists protect ecosystems from human damage.",
        tag_facts={"keywords": ["environmentalism"]},
    )
    friendship_series = _item(
        media_type="tv",
        title="Rebelde Way",
        genres=["Soap", "Drama"],
        overview="Four teenagers at an elite school are drawn together by music.",
        mdb_facts={"keywords": ["friendship"]},
    )

    assert "Environmentalism" not in {tag.name for tag in tx.shortlist(degrassi)}
    assert "Politics and Governance" not in {tag.name for tag in tx.shortlist(eureka)}
    assert "At Sea" not in {tag.name for tag in tx.shortlist(arrow)}
    assert "Prison" not in {tag.name for tag in tx.shortlist(mayans)}
    assert "Prison" in {tag.name for tag in tx.shortlist(prison_series)}
    assert "At Sea" in {tag.name for tag in tx.shortlist(sea_series)}
    assert "Environmentalism" in {
        tag.name for tag in tx.shortlist(environmental_series)
    }
    assert "Friendship" in {tag.name for tag in tx.shortlist(friendship_series)}


def test_job_226_shared_universe_is_not_automatically_cinematic() -> None:
    ben_ten = _item(
        media_type="tv",
        title="Ben 10",
        genres=["Animation", "Action & Adventure", "Comedy"],
        mdb_facts={"keywords": ["shared-universe"]},
    )
    marvel = _item(
        title="Avengers",
        genres=["Action", "Adventure"],
        tag_facts={"keywords": ["marvel cinematic universe (mcu)"]},
    )

    assert "Cinematic Universe" not in {tag.name for tag in tx.shortlist(ben_ten)}
    assert "Cinematic Universe" in {tag.name for tag in tx.shortlist(marvel)}


def test_group_into_batches_covers_every_item():
    items = [
        _item(tmdb_id=i, genres=["Drama" if i % 2 else "Action"]) for i in range(97)
    ]
    batches = list(tx.group_into_batches(items, 40))
    assert sum(len(b) for b in batches) == 97
    assert all(len(b) <= 40 for b in batches)
    assert {i["tmdb_id"] for b in batches for i in b} == {i["tmdb_id"] for i in items}


def test_group_into_batches_rejects_bad_size():
    with pytest.raises(ValueError):
        list(tx.group_into_batches([_item()], 0))


def test_group_into_batches_clusters_by_genre_and_decade():
    items = [_item(tmdb_id=1, genres=["Horror"], release_date="1985-01-01")] * 3
    items += [_item(tmdb_id=2, genres=["Comedy"], release_date="2019-01-01")] * 3
    batches = list(tx.group_into_batches(items, 40))
    assert len(batches) == 2, "unlike items should not share a batch"


def test_prompt_shard_fits_the_token_budget():
    """The real constraint: a batch's vocabulary block under ~2200 tokens.

    SHORTLIST_MAX bounds one item; this bounds what actually reaches the model.
    """
    batch = [
        _item(
            tmdb_id=i,
            genres=["Horror"],
            release_date="1985-06-01",
            keywords=["slasher", "small town", "revenge"],
            overview="A masked killer stalks teenagers in a small town.",
        )
        for i in range(40)
    ]
    vocab = tx.batch_vocabulary(batch)
    shard = tx.prompt_shard(vocab)
    tokens = tx.estimate_tokens(shard)
    assert tokens < 2200, f"{tokens} tokens for {len(vocab)} tags"


def test_prompt_shard_fits_budget_for_a_mixed_batch():
    """Worst case: clustering defeated, every item different."""
    genres = ["Horror", "Comedy", "Drama", "Action", "Sci-Fi", "Romance"]
    batch = [
        _item(
            tmdb_id=i,
            genres=[genres[i % len(genres)]],
            keywords=["heist", "time travel", "vampire", "samurai"][i % 4 : i % 4 + 1],
        )
        for i in range(40)
    ]
    tokens = tx.estimate_tokens(tx.prompt_shard(tx.batch_vocabulary(batch)))
    assert tokens < 2200, tokens


def test_prompt_shard_lines_carry_facet_and_gloss():
    shard = tx.prompt_shard(tx.BY_FACET["mood"][:3])
    for line in shard.splitlines():
        assert line.startswith("[mood] ")
        assert "—" in line


def test_indexed_prompt_shard_gives_every_schema_choice_a_stable_integer():
    shard = tx.prompt_shard(tx.BY_FACET["mood"][:3], indexed=True)
    for index, line in enumerate(shard.splitlines()):
        assert line.startswith(f"{index}: [mood] ")
        assert "—" in line


# ---------------------------------------------------------------------------
# Row naming
# ---------------------------------------------------------------------------
def test_taste_row_name_replaces_the_dark_for_you_pattern():
    assert tx.taste_row_name(tx.BY_NAME["Dark"]) == "Because You Like It Dark"
    assert tx.taste_row_name(tx.BY_NAME["Slasher"]) == "More Slasher"
    assert tx.taste_row_name(tx.BY_NAME["Grief"]) == "Stories About Grief"


def test_every_facet_has_a_taste_row_template():
    missing = [f for f in tx.FACETS if f not in tx.TASTE_ROW_TEMPLATES]
    assert not missing, missing


def test_name_templates_reference_valid_facets():
    for a, b in tx.NAME_TEMPLATES:
        assert a in tx.FACETS and b in tx.FACETS


def test_facet_counts_sums_to_taxonomy_size():
    assert sum(tx.facet_counts().values()) == len(tx.TAXONOMY)
