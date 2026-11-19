"""
Tests for the tag validation layer.

The behaviour that matters most is the one the old code did not have: a tag the
model returns that cannot be resolved is *recorded*, never silently dropped.
The pre-2.0 workers did ``if tag_name in tag_map:`` and discarded everything
else without a log line, so nobody could tell whether off-vocabulary output was
2% or 40% of what the model produced.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import taxonomy as tx  # noqa: E402
from app.tag_validation import (  # noqa: E402
    DEFAULT_MAX_PER_ITEM,
    DEFAULT_MIN_CONFIDENCE,
    FACET_MIN_CONFIDENCE,
    MAX_MODEL_TAGS,
    MIN_SURVIVORS,
    MODEL_FACET_CAPS,
    normalize,
    resolve_tag,
    validate_item_tags,
)

SHORTLIST = [t.name for t in tx.TAXONOMY if not t.derived]


# ---------------------------------------------------------------------------
# resolve_tag: the four-step chain
# ---------------------------------------------------------------------------
def test_exact_canonical():
    tag, method = resolve_tag("Neo-Noir")
    assert tag is not None and tag.name == "Neo-Noir"
    assert method == "exact"


def test_normalized_match():
    tag, method = resolve_tag("  neo   NOIR ")
    assert tag is not None and tag.name == "Neo-Noir"
    assert method == "normalized"


def test_alias_match():
    tag, method = resolve_tag("science fiction")
    assert tag is not None and tag.name == "Sci-Fi"
    assert method == "alias"


def test_db_alias_wins_over_static():
    """Operator-managed aliases must be able to override the taxonomy."""
    tag, method = resolve_tag(
        "science fiction", db_aliases={normalize("science fiction"): "Documentary"}
    )
    assert tag is not None and tag.name == "Documentary"
    assert method == "db_alias"


def test_db_alias_pointing_at_unknown_tag_is_ignored():
    tag, _ = resolve_tag(
        "science fiction", db_aliases={"science fiction": "No Such Tag"}
    )
    assert tag is not None and tag.name == "Sci-Fi"


def test_fuzzy_match_within_shortlist():
    """'Neo Noir' -> 'Neo-Noir' is the case fuzzy exists for."""
    tag, method = resolve_tag("Neo  Noir", shortlist_names=["Neo-Noir", "Heist"])
    assert tag is not None and tag.name == "Neo-Noir"
    # normalization already fixes this one; fuzzy is the backstop
    assert method in {"normalized", "fuzzy"}


def test_fuzzy_handles_a_near_miss_spelling():
    tag, method = resolve_tag("Cyberpnk", shortlist_names=["Cyberpunk", "Heist"])
    assert tag is not None and tag.name == "Cyberpunk"
    assert method == "fuzzy"


@pytest.mark.parametrize(
    "raw,offered",
    [
        # Shares a word with the offered tag but is a different concept.
        ("Space Horror", ["Space Opera"]),
        ("Slasher Musical", ["Slasher"]),
        ("Heist Documentary", ["Heist Thriller"]),
    ],
)
def test_fuzzy_does_not_overreach(raw, offered):
    """A distinct concept must not be absorbed into a similar-looking tag.

    This is why fuzzy matching is capped at 0.90 and confined to the item's own
    shortlist -- a looser rule turns every unfamiliar phrase into its nearest
    neighbour, which is worse than recording it as off-vocabulary.
    """
    assert raw not in tx.BY_NAME, "test premise: the raw string must not be a real tag"
    tag, _ = resolve_tag(raw, shortlist_names=offered)
    assert tag is None


def test_fuzzy_is_disabled_without_a_shortlist():
    assert resolve_tag("Cyberpnk")[0] is None


def test_fuzzy_is_confined_to_the_shortlist():
    """A tag the model was never offered should not be fuzzily reachable."""
    tag, _ = resolve_tag("Cyberpnk", shortlist_names=["Heist", "Revenge"])
    assert tag is None


@pytest.mark.parametrize(
    "raw,offered",
    [
        ("Space Opera", ["Heist", "Dark"]),
        ("science fiction", ["Heist", "Dark"]),
    ],
)
def test_validator_rejects_exact_and_alias_tags_outside_title_shortlist(raw, offered):
    """The batch-union schema must not leak a neighbour's legal tag."""
    result = validate_item_tags(603, "movie", [[raw, 0.95]], shortlist_names=offered)
    assert not result.accepted
    assert any(r.reason == "not offered for title" for r in result.rejected)


def test_validator_rejects_db_alias_target_outside_title_shortlist():
    result = validate_item_tags(
        603,
        "movie",
        [["cosmic adventure", 0.95]],
        shortlist_names=["Heist", "Dark"],
        db_aliases={normalize("cosmic adventure"): "Space Opera"},
    )
    assert not result.accepted
    assert any(r.reason == "not offered for title" for r in result.rejected)


@pytest.mark.parametrize("raw", ["", "   ", None])
def test_empty_input(raw):
    assert resolve_tag(raw)[0] is None


def test_unknown_string_is_unmapped():
    tag, method = resolve_tag("Vibes Based Cinema", shortlist_names=SHORTLIST)
    assert tag is None and method == "unmapped"


def test_every_legacy_name_resolves():
    from tests.test_taxonomy import LEGACY_TAG_NAMES

    for name in LEGACY_TAG_NAMES:
        tag, _ = resolve_tag(name)
        assert tag is not None and tag.name == name, name


# ---------------------------------------------------------------------------
# validate_item_tags
# ---------------------------------------------------------------------------
def _validate(raw_tags, **kw):
    kw.setdefault("shortlist_names", SHORTLIST)
    return validate_item_tags(603, "movie", raw_tags, **kw)


def test_garbage_is_recorded_not_dropped():
    """The whole reason this module exists."""
    result = _validate([["Heist", 0.9], ["Vibes Based Cinema", 0.8], ["Nonsense", 0.7]])
    assert {r.raw for r in result.unresolved} == {"Vibes Based Cinema", "Nonsense"}
    assert [r.tag.name for r in result.accepted] == ["Heist"]
    assert result.stats["unresolved"] == 2


def test_accepts_array_of_pairs():
    result = _validate([["Heist", 0.95], ["Dark", 0.8]])
    assert dict(result.rows) == {"Heist": 0.95, "Dark": 0.8}


def test_accepts_legacy_object_shape():
    """A prompt-format change must not silently empty a batch."""
    result = _validate({"Heist": 0.95, "Dark": 0.8})
    assert dict(result.rows) == {"Heist": 0.95, "Dark": 0.8}


def test_accepts_dict_entries():
    result = _validate([{"name": "Heist", "confidence": 0.9}])
    assert dict(result.rows) == {"Heist": 0.9}


def test_confidence_is_clamped():
    result = _validate([["Heist", 1.7], ["Revenge", -0.2]])
    accepted = dict(result.rows)
    assert accepted["Heist"] == 1.0
    assert "Revenge" not in accepted  # clamped to 0.0, then below the floor


def test_unparseable_confidence_is_rejected_not_crashed():
    result = _validate([["Heist", "very high"], ["Revenge", None], ["Detective", 0.9]])
    assert dict(result.rows) == {"Detective": 0.9}
    assert len(result.rejected) == 2


def test_confidence_floor():
    result = _validate([["Heist", 0.9], ["Revenge", 0.2]], min_confidence=0.45)
    assert dict(result.rows) == {"Heist": 0.9}
    assert any(r.reason == "below confidence floor" for r in result.rejected)


def test_default_confidence_floor_rejects_job_223_speculation():
    result = _validate(
        [["Feel-Good", 0.55], ["Survival", 0.85], ["Zombie Horror", 0.95]]
    )

    assert DEFAULT_MIN_CONFIDENCE == 0.60
    assert dict(result.rows) == {"Survival": 0.85, "Zombie Horror": 0.95}
    assert any(
        r.raw == "Feel-Good" and r.reason == "below facet confidence floor"
        for r in result.rejected
    )


def test_subjective_facets_reject_job_224_boundary_filler() -> None:
    result = _validate(
        [
            ["Suspenseful", 0.60],
            ["Fast-Paced", 0.60],
            ["Memory", 0.60],
            ["Heist", 0.85],
        ]
    )

    assert FACET_MIN_CONFIDENCE == {"mood": 0.65, "pacing_format": 0.65}
    assert dict(result.rows) == {"Heist": 0.85, "Memory": 0.60}
    rejected = {resolution.raw: resolution.reason for resolution in result.rejected}
    assert rejected["Suspenseful"] == "below facet confidence floor"
    assert rejected["Fast-Paced"] == "below facet confidence floor"


def test_subjective_facets_accept_clear_assignments_at_their_floor() -> None:
    result = _validate([["Suspenseful", 0.65], ["Fast-Paced", 0.65]])

    assert dict(result.rows) == {"Fast-Paced": 0.65, "Suspenseful": 0.65}


def test_per_item_cap_keeps_the_highest_confidence():
    raw = [[t.name, 0.7 + i / 1000] for i, t in enumerate(tx.BY_FACET["mood"])]
    result = _validate(raw, max_per_item=5)
    assert len(result.accepted) == 1
    kept = [r.confidence for r in result.accepted]
    assert kept == sorted(kept, reverse=True)
    assert any(r.reason == "facet cap" for r in result.rejected)


def test_model_output_caps_match_the_prompt_contract() -> None:
    raw = [
        ["Heist", 0.99],
        ["Based on a Book", 0.98],
        ["Strong Female Lead", 0.97],
        ["New York City", 0.96],
        ["World War II", 0.95],
        ["Suspenseful", 0.94],
    ]
    result = _validate(raw)

    assert MAX_MODEL_TAGS == 3
    assert MODEL_FACET_CAPS["mood"] == 1
    assert len(result.accepted) == 3
    assert any(
        r.raw == "New York City" and r.reason == "over cap" for r in result.rejected
    )


def test_default_cap_is_applied():
    raw = [[t.name, 0.9] for t in tx.BY_FACET["mood"] + tx.BY_FACET["character"]]
    assert len(_validate(raw).accepted) <= DEFAULT_MAX_PER_ITEM


def test_media_type_gating():
    """Sitcom is tv-only; it must not attach to a movie."""
    result = validate_item_tags(
        1,
        "movie",
        [["Sitcom", 0.9], ["Heist", 0.9]],
        shortlist_names=["Sitcom", "Heist"],
    )
    assert dict(result.rows) == {"Heist": 0.9}
    assert any(r.reason == "media type not allowed" for r in result.rejected)


def test_model_supplied_derived_tags_are_rejected():
    """The model is never offered derived tags; a guessed decade is not trusted."""
    result = validate_item_tags(
        1, "movie", [["1990s", 0.9], ["Heist", 0.9]], shortlist_names=["Heist"]
    )
    assert dict(result.rows) == {"Heist": 0.9}
    assert any(r.reason == "derived tag from model" for r in result.rejected)


def test_computed_derived_tags_are_merged():
    result = _validate(
        [["Heist", 0.9]],
        derived=tx.derive_tags(
            {"media_type": "movie", "release_date": "1995-01-01", "countries": ["US"]}
        ),
    )
    rows = dict(result.rows)
    assert rows["Heist"] == 0.9
    assert rows["1990s"] == 1.0
    assert rows["American"] == 1.0


def test_derived_tags_survive_the_cap():
    """Derived tags are facts; the model's judgements get trimmed instead."""
    raw = [[t.name, 0.9] for t in tx.BY_FACET["mood"]]
    derived = tx.derive_tags({"media_type": "movie", "release_date": "1995-01-01"})
    result = _validate(raw, derived=derived, max_per_item=5)
    assert "1990s" in dict(result.rows)
    assert len(result.accepted) == 2


def test_derived_tags_respect_media_type():
    result = validate_item_tags(
        1, "movie", [["Heist", 0.9]], derived={"Limited Series": 1.0}
    )
    assert "Limited Series" not in dict(result.rows)


def test_duplicates_keep_the_highest_confidence():
    result = _validate([["Heist", 0.6], ["heist", 0.95], ["  HEIST ", 0.7]])
    assert dict(result.rows) == {"Heist": 0.95}


def test_requeue_when_too_few_survive():
    result = _validate([["Nonsense", 0.9]])
    assert result.requeue is True


def test_model_output_is_optional_when_any_valid_tag_exists():
    result = _validate([["Heist", 0.9], ["Nonsense", 0.9]])

    assert MIN_SURVIVORS == 0
    assert result.requeue is False


def test_no_requeue_with_enough_tags():
    raw = [["Heist", 0.9], ["Action", 0.9], ["Friendship", 0.9], ["Dark", 0.9]]
    result = _validate(raw)
    assert len(result.accepted) >= MIN_SURVIVORS
    assert result.requeue is False


def test_semantic_duplicates_canonicalize_to_one_concept():
    result = validate_item_tags(
        1,
        "movie",
        [["Coming-of-Age", 0.8], ["Coming of Age", 0.9]],
        shortlist_names=None,
    )
    assert dict(result.rows) == {"Coming of Age": 0.9}


def test_conflicting_tags_keep_only_the_stronger_assignment():
    result = validate_item_tags(
        1,
        "movie",
        [
            ["Slow-Burn", 0.8],
            ["Fast-Paced", 0.95],
            ["Friendship", 0.8],
        ],
        shortlist_names=None,
    )
    assert "Fast-Paced" in dict(result.rows)
    assert "Slow-Burn" not in dict(result.rows)
    assert any(r.reason == "conflicts with Fast-Paced" for r in result.rejected)


def test_opposed_moods_from_job_221_do_not_survive_together() -> None:
    result = validate_item_tags(
        1,
        "movie",
        [["Chilling", 0.8], ["Lighthearted", 0.9], ["Friendship", 0.8]],
        shortlist_names=None,
    )
    assert "Lighthearted" in dict(result.rows)
    assert "Chilling" not in dict(result.rows)


def test_one_model_tag_plus_derived_facts_satisfies_the_survivor_floor():
    """One precise judgement is enough alongside deterministic facts."""
    derived = tx.derive_tags(
        {
            "media_type": "movie",
            "release_date": "1995-01-01",
            "countries": ["US"],
            "runtime": 120,
        }
    )
    assert len(derived) >= 3
    result = _validate([["Heist", 0.9]], derived=derived)
    assert result.requeue is False


def test_empty_input_requeues():
    result = _validate([])
    assert result.accepted == [] and result.requeue is True


def test_rows_are_upsert_ready():
    result = _validate([["Heist", 0.9], ["Revenge", 0.8]])
    for name, confidence in result.rows:
        assert name in tx.BY_NAME
        assert 0.0 <= confidence <= 1.0


def test_result_carries_item_identity():
    result = validate_item_tags(603, "movie", [["Heist", 0.9]])
    assert result.tmdb_id == 603 and result.media_type == "movie"
