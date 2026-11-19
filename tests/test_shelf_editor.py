# ruff: noqa: E402
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("TMDB_API_KEY", "test_key")
os.environ.setdefault("GEMINI_API_KEY", "test_key")
os.environ.setdefault("MASTER_PASSWORD", "test_password")
os.environ.setdefault("SECRET_KEY", "test_secret_key_at_least_32_chars_long")
os.environ.setdefault("BASE_URL", "http://localhost:8000")
os.environ.setdefault("SKIP_API_VALIDATION", "true")

from app.category_synth import (
    RowSpec,
    _CONCEPT_DEDUPE_FACETS,
    _item_fits_shelf,
    _unsupported_name_concepts,
    compile_shelves,
    name_for,
)
from app.models import Base
from app.shelf_editor import (
    EditorialResult,
    ShelfEditorialError,
    editor_provider_options,
    review_candidates,
    validate_editor_model,
)
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def _candidate(title: str = "Example") -> dict:
    return {
        "candidate_id": "candidate-1",
        "spec_hash": "a" * 40,
        "deterministic_name": "Dark Crime Movies",
        "media_type": "movie",
        "tags": ["Dark", "Crime"],
        "facets": ["mood", "subgenre"],
        "support": 80,
        "lift": 2.1,
        "actual_item_count": 40,
        "sample_titles": [
            {
                "tmdb_id": 1,
                "title": title,
                "year": "2020",
                "quality": 8.1,
            }
        ],
    }


def _openai_response(name: str = "Dark Crime Stories") -> dict:
    return {
        "output": [
            {
                "content": [
                    {
                        "type": "output_text",
                        "text": (
                            '{"decisions":[{"candidate_id":"candidate-1",'
                            '"decision":"keep","display_name":"'
                            + name
                            + '","editorial_score":88,'
                            '"reason_codes":["strong_concept"]}]}'
                        ),
                    }
                ]
            }
        ],
        "usage": {"input_tokens": 200, "output_tokens": 50},
    }


def test_editor_caches_keep_and_invalidates_only_changed_samples(db, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "synth_editor_provider", "openai")
    monkeypatch.setattr(settings, "synth_editor_model", "gpt-5.4-nano")
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")
    monkeypatch.setattr(settings, "synth_editor_cost_ceiling_usd", 0.25)
    calls = []

    def request(*args):
        calls.append(args)
        return _openai_response(), 200, 50

    first = review_candidates(db, [_candidate()], request_fn=request)
    second = review_candidates(db, [_candidate()], request_fn=request)
    changed = review_candidates(db, [_candidate("Changed sample")], request_fn=request)

    assert first.reviewed == 1 and first.cached == 0
    assert second.reviewed == 0 and second.cached == 1
    assert changed.reviewed == 1 and changed.cached == 0
    assert len(calls) == 2


def test_editor_reserves_the_hard_ceiling_before_calling_provider(db, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "synth_editor_provider", "openai")
    monkeypatch.setattr(settings, "synth_editor_model", "gpt-5.4-nano")
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")
    monkeypatch.setattr(settings, "synth_editor_cost_ceiling_usd", 0.0001)
    request = MagicMock()

    with pytest.raises(ShelfEditorialError, match="ceiling"):
        review_candidates(db, [_candidate()], request_fn=request)
    request.assert_not_called()


def _spec(slug: str, name: str, tag_a: str, tag_b: str) -> RowSpec:
    return RowSpec(
        slug=slug,
        name=name,
        media_type="movie",
        tag_a=tag_a,
        tag_b=tag_b,
        facet_a="mood",
        facet_b="subgenre",
        support=80,
        lift=2.0,
    )


def test_compiler_rejects_29_items_and_accepts_30(monkeypatch):
    specs = [
        _spec("thin", "Thin Crime Movies", "Bleak", "Crime"),
        _spec("dense", "Dense Horror Movies", "Dark", "Horror"),
    ]
    monkeypatch.setattr("app.category_synth.propose", lambda *_args: specs)
    monkeypatch.setattr("app.category_synth._candidate_samples", lambda *_args: {})
    calls = iter(
        [
            [(value, 1.0) for value in range(1, 30)],
            [(value, 1.0) for value in range(100, 130)],
            [(value, 1.0) for value in range(1, 30)],
            [(value, 1.0) for value in range(100, 130)],
        ]
    )
    monkeypatch.setattr(
        "app.category_synth.CatalogGenerator.generate_universal_catalog",
        lambda *_args, **_kwargs: next(calls),
    )

    result = compile_shelves(MagicMock(), limit_per_type=10, use_ai=False)

    assert [shelf.spec.slug for shelf in result.shelves] == ["dense"]
    assert any(row["reason_codes"] == ["too_thin"] for row in result.rejected)


def test_ai_can_rename_but_cannot_change_formula_or_membership(monkeypatch):
    spec = _spec("crime", "Dark Crime Movies", "Dark", "Crime")
    ranked = [(value, 1.0) for value in range(1, 41)]
    monkeypatch.setattr("app.category_synth.propose", lambda *_args: [spec])
    monkeypatch.setattr("app.category_synth._candidate_samples", lambda *_args: {})
    monkeypatch.setattr(
        "app.category_synth.CatalogGenerator.generate_universal_catalog",
        lambda *_args, **_kwargs: ranked,
    )
    monkeypatch.setattr(
        "app.category_synth.review_candidates",
        lambda *_args, **_kwargs: EditorialResult(
            decisions={
                "crime": {
                    "decision": "keep",
                    "display_name": "Dark Crime Stories",
                    "editorial_score": 90,
                    "reason_codes": ["strong_concept"],
                    "cached": False,
                }
            },
            cached=0,
            reviewed=1,
            input_tokens=1,
            output_tokens=1,
            cost_usd=0.01,
            reserved_cost_usd=0.02,
            provider="openai",
            model="gpt-5.4-nano",
        ),
    )

    result = compile_shelves(MagicMock(), limit_per_type=10, use_ai=True)
    shelf = result.shelves[0]
    assert shelf.display_name == "Dark Crime Stories"
    assert shelf.spec.formula == spec.formula
    assert list(shelf.ranked) == ranked


def test_invalid_ai_name_cannot_publish(monkeypatch):
    spec = _spec("crime", "Dark Crime Movies", "Dark", "Crime")
    monkeypatch.setattr("app.category_synth.propose", lambda *_args: [spec])
    monkeypatch.setattr("app.category_synth._candidate_samples", lambda *_args: {})
    monkeypatch.setattr(
        "app.category_synth.CatalogGenerator.generate_universal_catalog",
        lambda *_args, **_kwargs: [(value, 1.0) for value in range(1, 41)],
    )
    monkeypatch.setattr(
        "app.category_synth.review_candidates",
        lambda *_args, **_kwargs: EditorialResult(
            decisions={
                "crime": {
                    "decision": "keep",
                    "display_name": "x" * 46,
                    "editorial_score": 90,
                    "reason_codes": [],
                    "cached": False,
                }
            },
            cached=0,
            reviewed=1,
            input_tokens=1,
            output_tokens=1,
            cost_usd=0.01,
            reserved_cost_usd=0.02,
            provider="openai",
            model="gpt-5.4-nano",
        ),
    )

    result = compile_shelves(MagicMock(), limit_per_type=10, use_ai=True)
    assert result.shelves == ()
    assert result.rejected[0]["reason_codes"] == ["invalid_name"]


def test_item_contract_removes_wrong_formats_and_mature_family_titles():
    dark_comedy = _spec("dark-comedy", "Dark Comedies", "Dark", "Comedy")
    family = RowSpec(
        slug="family",
        name="Family Shows for Kids",
        media_type="tv",
        tag_a="Family",
        tag_b="Kids",
        facet_a="subgenre",
        facet_b="audience",
        support=80,
        lift=2.0,
    )
    item = SimpleNamespace(
        title="Example",
        overview="",
        genres=["Comedy"],
        mdb_facts={},
        tag_facts={},
        adult=False,
    )
    assert not _item_fits_shelf(dark_comedy, item, {"Stand-Up Special"})

    item.genres = ["Science Fiction", "Documentary"]
    assert not _item_fits_shelf(dark_comedy, item, set())

    item.genres = ["Crime", "Drama"]
    item.mdb_facts = {"certification": "TV-MA"}
    assert not _item_fits_shelf(family, item, set())


@pytest.mark.parametrize(
    ("genres", "overview", "tag_facts"),
    [
        ([99], "", {}),
        ([{"id": 99}], "", {}),
        (["Drama"], "An intimate feature documentary about a city.", {}),
        (["Drama"], "", {"keywords": ["non-fiction"]}),
    ],
)
def test_documentary_evidence_cannot_leak_into_narrative_shelves(
    genres, overview, tag_facts
):
    narrative = _spec("london", "British Movies Set in London", "British", "London")
    item = SimpleNamespace(
        title="Example",
        overview=overview,
        genres=genres,
        mdb_facts={},
        tag_facts=tag_facts,
        adult=False,
    )

    assert not _item_fits_shelf(narrative, item, set())


def test_content_contract_is_applied_before_the_30_item_floor(monkeypatch):
    spec = _spec("dark-comedy", "Dark Comedies", "Dark", "Comedy")
    stand_up = SimpleNamespace(
        title="A Comedy Special",
        overview="One comedian performs on stage.",
        genres=["Comedy"],
        mdb_facts={},
        tag_facts={},
        adult=False,
    )
    monkeypatch.setattr("app.category_synth.propose", lambda *_args: [spec])
    monkeypatch.setattr(
        "app.category_synth._candidate_samples",
        lambda *_args: {("movie", 1): stand_up},
    )
    monkeypatch.setattr(
        "app.category_synth._candidate_item_tags",
        lambda *_args: {("movie", 1): {"Stand-Up Special"}},
    )
    monkeypatch.setattr(
        "app.category_synth.CatalogGenerator.generate_universal_catalog",
        lambda *_args, **_kwargs: [(value, 1.0) for value in range(1, 31)],
    )

    result = compile_shelves(MagicMock(), limit_per_type=10, use_ai=False)

    assert result.shelves == ()
    assert result.rejected[0]["actual_item_count"] == 29
    assert result.rejected[0]["reason_codes"] == ["too_thin"]


def test_ai_name_cannot_add_taxonomy_concepts():
    spec = _spec("cg", "CG-Animated Movies", "CG Animation", "Animation")

    assert _unsupported_name_concepts("CG-Animated Movies", spec) == []
    assert _unsupported_name_concepts("CG-Animated Adventure & Comedy", spec) == [
        "Adventure",
        "Comedy",
    ]


def test_runtime_names_describe_length_not_narrative_scale():
    name, _, _ = name_for("Indian", "origin", "Epic Runtime", "pacing_format", "movie")

    assert name == "Epic-Length Indian Movies"


def test_semantic_dedupe_covers_repeated_style_source_and_pacing_concepts():
    assert {"style", "source_franchise", "pacing_format"}.issubset(
        _CONCEPT_DEDUPE_FACETS
    )


def test_premium_models_are_editor_only():
    from app.tagging_provider import PROVIDER_SPECS, validate_model

    options = {option["id"]: option for option in editor_provider_options()}
    editor_ids = {model["id"] for model in options["openai"]["models"]}
    tagging_ids = {model["id"] for model in PROVIDER_SPECS["openai"]["models"]}

    assert {"gpt-5.4-mini", "gpt-5.6-terra", "gpt-5.6-sol"} <= editor_ids
    assert "gpt-5.4-mini" not in tagging_ids
    assert validate_editor_model("openai", "gpt-5.4-mini") == "gpt-5.4-mini"
    with pytest.raises(ValueError, match="Unsupported"):
        validate_model("openai", "gpt-5.4-mini")


def test_low_scoring_ai_keep_is_still_rejected(monkeypatch):
    spec = _spec("crime", "Dark Crime Movies", "Dark", "Crime")
    monkeypatch.setattr("app.category_synth.propose", lambda *_args: [spec])
    monkeypatch.setattr("app.category_synth._candidate_samples", lambda *_args: {})
    monkeypatch.setattr(
        "app.category_synth.CatalogGenerator.generate_universal_catalog",
        lambda *_args, **_kwargs: [(value, 1.0) for value in range(1, 41)],
    )
    monkeypatch.setattr(
        "app.category_synth.review_candidates",
        lambda *_args, **_kwargs: EditorialResult(
            decisions={
                "crime": {
                    "decision": "keep",
                    "display_name": "Dark Crime Stories",
                    "editorial_score": 74,
                    "reason_codes": ["strong_concept"],
                    "cached": False,
                }
            },
            cached=0,
            reviewed=1,
            input_tokens=1,
            output_tokens=1,
            cost_usd=0.01,
            reserved_cost_usd=0.02,
            provider="openai",
            model="gpt-5.4-nano",
        ),
    )

    result = compile_shelves(MagicMock(), limit_per_type=10, use_ai=True)

    assert result.shelves == ()
    assert result.rejected[0]["reason_codes"] == ["low_editorial_score"]


def test_specific_browse_concept_publishes_only_once_per_media_type(monkeypatch):
    crime = RowSpec(
        slug="detective-crime",
        name="Detective Crime Movies",
        media_type="movie",
        tag_a="Detective",
        tag_b="Crime",
        facet_a="plot_device",
        facet_b="subgenre",
        support=90,
        lift=3.0,
    )
    mystery = RowSpec(
        slug="detective-mystery",
        name="Detective Mystery Movies",
        media_type="movie",
        tag_a="Detective",
        tag_b="Mystery",
        facet_a="plot_device",
        facet_b="subgenre",
        support=80,
        lift=2.5,
    )
    monkeypatch.setattr("app.category_synth.propose", lambda *_args: [crime, mystery])
    monkeypatch.setattr("app.category_synth._candidate_samples", lambda *_args: {})

    def ranked(_generator, category, **_kwargs):
        start = 1 if category.id == crime.slug else 100
        return [(value, 1.0) for value in range(start, start + 40)]

    monkeypatch.setattr(
        "app.category_synth.CatalogGenerator.generate_universal_catalog", ranked
    )
    monkeypatch.setattr(
        "app.category_synth.review_candidates",
        lambda *_args, **_kwargs: EditorialResult(
            decisions={
                crime.slug: {
                    "decision": "keep",
                    "display_name": crime.name,
                    "editorial_score": 90,
                    "reason_codes": ["strong_concept"],
                    "cached": False,
                },
                mystery.slug: {
                    "decision": "keep",
                    "display_name": mystery.name,
                    "editorial_score": 85,
                    "reason_codes": ["strong_concept"],
                    "cached": False,
                },
            },
            cached=0,
            reviewed=2,
            input_tokens=1,
            output_tokens=1,
            cost_usd=0.01,
            reserved_cost_usd=0.02,
            provider="openai",
            model="gpt-5.4-nano",
        ),
    )

    result = compile_shelves(MagicMock(), limit_per_type=10, use_ai=True)

    assert [shelf.spec.slug for shelf in result.shelves] == [crime.slug]
    assert any(row["reason_codes"] == ["duplicate_concept"] for row in result.rejected)
