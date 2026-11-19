# ruff: noqa: E402
"""The tag QA endpoints — reading the signal the tagger has been writing.

The last re-tag recorded **263,512** off-vocabulary suggestions against 112,418
titles, every one stored in ``tag_observations`` with an occurrence count and up
to 10 sample ids. Until these endpoints existed none of it was readable: the only
exposure was a single aggregate scalar on the coverage card.

That matters because it is the instrument for deciding whether to buy another
re-tag. The plan told the operator to consult it before spending, and then
shipped without the endpoint.
"""

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_tag_qa_api.db")
os.environ.setdefault("TMDB_API_KEY", "test_key")
os.environ.setdefault("GEMINI_API_KEY", "test_key")
os.environ.setdefault("MASTER_PASSWORD", "test_password")
os.environ.setdefault("SECRET_KEY", "test_secret_key_at_least_32_chars_long")
os.environ.setdefault("BASE_URL", "http://localhost:8000")
os.environ.setdefault("SKIP_API_VALIDATION", "true")

from app.admin import verify_admin  # noqa: E402
from app.database import get_db, init_database  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    AdminSetting,
    MediaMetadata,
    MovieTag,
    Tag,
    TagAlias,
    TagObservation,
    TagStats,
    UniversalCategory,
)


@pytest.fixture()
def client():
    # verify_admin is overridden rather than logging in per test: the login
    # endpoint is rate limited, so a file's worth of logins starts returning 401
    # partway through and every later assertion fails for the wrong reason.
    # Same pattern as tests/test_tagging_admin_api.py.
    app.dependency_overrides[verify_admin] = lambda: True
    init_database()
    with TestClient(app, base_url="https://testserver") as c:
        # Cleared INSIDE the client context: entering it fires the app's startup
        # event, which runs init_database() -> sync_taxonomy() and re-seeds the
        # full 491-tag taxonomy plus the 40 curated categories. Wiping before
        # that would be undone immediately.
        #
        # These tests need a small controlled vocabulary -- notably
        # test_dead_tags, whose entire assertion is about which tags are and are
        # not referenced by a formula.
        with get_db() as db:
            db.query(TagObservation).delete()
            db.query(TagAlias).delete()
            db.query(TagStats).delete()
            db.query(MovieTag).delete()
            db.query(UniversalCategory).delete()
            db.query(Tag).delete()
            db.query(MediaMetadata).delete()
            db.commit()
        yield c
    app.dependency_overrides.clear()


def _observe(db, raw, occurrences, media_type="movie", samples=None):
    from app.taxonomy import normalize

    db.add(
        TagObservation(
            normalized=normalize(raw)[:200],
            media_type=media_type,
            raw_name=raw,
            occurrences=occurrences,
            sample_tmdb_ids=samples or [],
            resolution="unmapped",
        )
    )


# ---------------------------------------------------------------------------
# Off-vocabulary
# ---------------------------------------------------------------------------
def test_offvocab_ranks_by_frequency_and_resolves_sample_titles(client):
    with get_db() as db:
        db.add(MediaMetadata(tmdb_id=603, media_type="movie", title="The Matrix"))
        _observe(db, "Dream Logic", 4210, samples=[603])
        _observe(db, "Slow Burn Revenge", 120)
        _observe(db, "One-off nonsense", 1)
        db.commit()

    body = client.get("/admin/api/tags/offvocab").json()

    assert body["total_observations"] == 3
    assert body["total_occurrences"] == 4331
    names = [i["raw_name"] for i in body["items"]]
    assert names == ["Dream Logic", "Slow Burn Revenge", "One-off nonsense"], (
        "the whole point is ranking by frequency — the top entries are the ones "
        f"worth acting on: {names}"
    )
    # A sample id resolved to a real title, so an operator can judge whether the
    # suggestion is a concept or a hallucination without leaving the page.
    assert body["items"][0]["samples"] == [{"tmdb_id": 603, "title": "The Matrix"}]


def test_offvocab_hides_already_resolved_entries_by_default(client):
    with get_db() as db:
        _observe(db, "Unresolved", 10)
        db.add(
            TagObservation(
                normalized="already",
                media_type="movie",
                raw_name="Already",
                occurrences=99,
                resolution="aliased",
            )
        )
        db.commit()

    default = client.get("/admin/api/tags/offvocab").json()
    assert [i["raw_name"] for i in default["items"]] == ["Unresolved"]

    everything = client.get("/admin/api/tags/offvocab?resolution=all").json()
    assert len(everything["items"]) == 2


# ---------------------------------------------------------------------------
# Alias promotion
# ---------------------------------------------------------------------------
def test_promoting_an_alias_resolves_it_for_future_tagging(client):
    with get_db() as db:
        db.add(Tag(id=1, name="Neo-Noir", category="subgenre"))
        _observe(db, "Neo Noir", 812)
        db.commit()

    body = client.post(
        "/admin/api/tags/alias", json={"raw": "Neo Noir", "tag_id": 1}
    ).json()

    assert body["status"] == "ok"
    assert body["tag"] == "Neo-Noir"
    assert body["occurrences"] == 812

    with get_db() as db:
        alias = db.query(TagAlias).filter(TagAlias.tag_id == 1).first()
        assert alias is not None
        assert alias.source == "admin", "operator aliases must outrank taxonomy ones"
        obs = db.query(TagObservation).first()
        assert obs.resolution == "aliased"
        assert obs.mapped_tag_id == 1


def test_alias_promotion_says_plainly_that_it_is_not_retroactive(client):
    """Nothing stores per-title raw model output, only aggregated observations.

    So a promoted alias helps the NEXT tagging run and leaves already-tagged
    titles untouched. The plan claimed "no redeploy needed" and left the
    retroactivity question unstated, which would lead an operator to expect the
    tag to appear on the 812 titles that suggested it.
    """
    with get_db() as db:
        db.add(Tag(id=1, name="Neo-Noir", category="subgenre"))
        _observe(db, "Neo Noir", 812)
        db.commit()

    body = client.post(
        "/admin/api/tags/alias", json={"raw": "Neo Noir", "tag_id": 1}
    ).json()

    assert body["retroactive"] is False
    assert "re-tag" in body["note"].lower()


def test_alias_promotion_rejects_a_missing_tag(client):
    resp = client.post("/admin/api/tags/alias", json={"raw": "x", "tag_id": 9999})
    assert resp.status_code == 404


def test_alias_promotion_requires_both_fields(client):
    assert client.post("/admin/api/tags/alias", json={"raw": "x"}).status_code == 400
    assert client.post("/admin/api/tags/alias", json={"tag_id": 1}).status_code == 400


def test_one_off_observation_cannot_be_promoted(client):
    with get_db() as db:
        db.add(Tag(id=1, name="Neo-Noir", category="subgenre"))
        _observe(db, "One-off idea", 1)
        db.commit()

    response = client.post(
        "/admin/api/tags/alias", json={"raw": "One-off idea", "tag_id": 1}
    )

    assert response.status_code == 400
    assert "only once" in response.json()["detail"]


# ---------------------------------------------------------------------------
# Dead tags
# ---------------------------------------------------------------------------
def test_dead_tags_separates_unassigned_from_unreferenced(client):
    """Two different failures needing opposite responses.

    A tag no title carries is a prompt/vocabulary problem. A tag titles DO carry
    but no row asks for is a *formula* problem — and that is the one that left 27
    of the original 75 tags driving no discovery while looking perfectly healthy.
    """
    with get_db() as db:
        db.add_all(
            [
                Tag(id=1, name="Assigned And Referenced", category="mood"),
                Tag(id=2, name="Assigned Not Referenced", category="mood"),
                Tag(id=3, name="Never Assigned", category="mood"),
            ]
        )
        db.add_all(
            [
                TagStats(tag_id=1, media_type="movie", doc_count=50, corpus_size=100),
                TagStats(tag_id=2, media_type="movie", doc_count=40, corpus_size=100),
                TagStats(tag_id=3, media_type="movie", doc_count=0, corpus_size=100),
            ]
        )
        db.add(
            UniversalCategory(
                id="c1",
                name="C1",
                tier=1,
                sort_order=1,
                media_type="movie",
                tag_formula={"required": ["Assigned And Referenced"]},
            )
        )
        db.commit()

    body = client.get("/admin/api/tags/dead").json()

    assert [t["name"] for t in body["zero_assignments"]] == ["Never Assigned"]
    assert [t["name"] for t in body["zero_category_refs"]] == [
        "Assigned Not Referenced"
    ]


def test_orphan_rows_is_zero_because_the_schema_forbids_them(client):
    """``movie_tags.tag_id`` is ForeignKey("tags.id", ondelete="CASCADE").

    So an orphan cannot be inserted -- the counter exists as an integrity check
    on the constraint itself, not as an expected condition. A non-zero value in
    production means the FK is missing, which would silently corrupt the
    dead-tag arithmetic above.
    """
    import sqlalchemy.exc

    with get_db() as db:
        db.add(Tag(id=1, name="Real", category="mood"))
        db.add(MovieTag(tmdb_id=1, tag_id=1, confidence=0.9, media_type="movie"))
        db.commit()

    assert client.get("/admin/api/tags/dead").json()["orphan_rows"] == 0

    with pytest.raises(sqlalchemy.exc.IntegrityError):
        with get_db() as db:
            db.add(MovieTag(tmdb_id=2, tag_id=777, confidence=0.9, media_type="movie"))
            db.commit()


# ---------------------------------------------------------------------------
# Tag table
# ---------------------------------------------------------------------------
def test_tag_list_reports_stats_and_which_rows_use_each_tag(client):
    with get_db() as db:
        db.add_all(
            [
                Tag(id=1, name="Heist", category="plot_device"),
                Tag(id=2, name="Bleak", category="mood"),
            ]
        )
        db.add(
            TagStats(
                tag_id=1,
                media_type="movie",
                doc_count=1200,
                corpus_size=50000,
                idf=3.7,
                avg_confidence=0.82,
            )
        )
        db.add(
            UniversalCategory(
                id="heists",
                name="Heists",
                tier=1,
                sort_order=1,
                media_type="movie",
                tag_formula={"required": ["Heist"], "optional": ["Bleak"]},
            )
        )
        db.commit()

    body = client.get("/admin/api/tags/list").json()
    by_name = {i["name"]: i for i in body["items"]}

    assert by_name["Heist"]["stats"]["movie"]["doc_count"] == 1200
    assert by_name["Heist"]["stats"]["movie"]["idf"] == 3.7
    assert by_name["Heist"]["category_refs"] == ["heists"]
    assert by_name["Bleak"]["category_refs"] == ["heists"]
    # Sorted by corpus frequency so the tags doing the most work come first.
    assert body["items"][0]["name"] == "Heist"


def test_tag_list_filters_by_facet_and_query(client):
    with get_db() as db:
        db.add_all(
            [
                Tag(id=1, name="Heist", category="plot_device"),
                Tag(id=2, name="Bleak", category="mood"),
            ]
        )
        db.commit()

    facet = client.get("/admin/api/tags/list?facet=mood").json()
    assert [i["name"] for i in facet["items"]] == ["Bleak"]

    search = client.get("/admin/api/tags/list?q=hei").json()
    assert [i["name"] for i in search["items"]] == ["Heist"]


# ---------------------------------------------------------------------------
# Toggle
# ---------------------------------------------------------------------------
def test_toggle_hides_a_tag_from_chips_without_touching_its_assignments(client):
    with get_db() as db:
        db.add(Tag(id=1, name="Gore", category="maturity", display=True))
        db.add(MovieTag(tmdb_id=1, tag_id=1, confidence=0.9, media_type="movie"))
        db.commit()

    assert client.post("/admin/api/tags/1/toggle").json()["display"] is False
    assert (
        client.post("/admin/api/tags/1/toggle").json()["display"] is True
    ), "the toggle must be reversible"

    with get_db() as db:
        assert (
            db.query(MovieTag).count() == 1
        ), "hiding a tag from chips must not delete what it is attached to"


def test_toggle_404s_on_an_unknown_tag(client):
    assert client.post("/admin/api/tags/4242/toggle").status_code == 404


# ---------------------------------------------------------------------------
# Relevance refresh
# ---------------------------------------------------------------------------
def test_relevance_refresh_populates_weights(client):
    """Previously reachable only via a full Upgrade or a shell into the box."""
    with get_db() as db:
        db.add(Tag(id=1, name="Heist", category="plot_device"))
        db.add(MediaMetadata(tmdb_id=1, media_type="movie", title="T"))
        db.add(MovieTag(tmdb_id=1, tag_id=1, confidence=0.9, media_type="movie"))
        db.commit()
        assert db.query(MovieTag).first().weight_norm is None

    assert client.post("/admin/api/relevance/refresh").json()["status"] == "ok"

    with get_db() as db:
        assert db.query(MovieTag).first().weight_norm is not None


# ---------------------------------------------------------------------------
# The serving cache
# ---------------------------------------------------------------------------
def test_regenerating_catalogs_clears_the_serving_cache(client):
    """Rows are served from an in-process LRU with a 1-hour TTL.

    Rebuilding them in the database without clearing it means nothing changes
    for a user for up to an hour -- which reads as "the button did nothing" and
    invites a second, equally invisible press. The upgrade pipeline always
    cleared it; this endpoint is the same operation behind a different button
    and was missed.
    """
    from app.main import _catalog_cache

    with get_db() as db:
        db.add(Tag(id=1, name="Heist", category="plot_device"))
        db.add(MediaMetadata(tmdb_id=1, media_type="movie", title="T"))
        db.add(MovieTag(tmdb_id=1, tag_id=1, confidence=0.9, media_type="movie"))
        db.commit()

    _catalog_cache["stale-key"] = (0.0, [{"tmdb_id": 1}])

    with patch(
        "app.category_synth.rebuild_synthesized_shelves",
        return_value={"published": 1},
    ):
        assert client.post("/admin/api/catalogs/regenerate").status_code == 200

    assert not _catalog_cache, "a rebuild that stays invisible for an hour is a bug"


def test_applying_synthesis_clears_the_serving_cache(client):
    """Deactivating a row must reach the serving layer immediately.

    Its content rows survive by design (deleting the category would cascade
    them away mid-flight), so a cached catalog would keep serving a row the
    operator just retired.
    """
    from app.main import _catalog_cache

    _catalog_cache["stale-key"] = (0.0, [{"tmdb_id": 1}])

    with patch(
        "app.category_synth.rebuild_synthesized_shelves",
        return_value={"published": 1},
    ):
        assert client.post("/admin/api/categories/synth/apply").status_code == 200

    assert not _catalog_cache


# ---------------------------------------------------------------------------
# Alias key length
# ---------------------------------------------------------------------------
def test_an_over_long_alias_is_rejected_rather_than_silently_broken(client):
    """Three places disagreed about this key's length.

    Observations are recorded at [:200], the alias column is String(160), and
    resolve_tag looks the key up UNTRUNCATED. Promoting an observation longer
    than 160 characters wrote an alias that could never match anything -- the
    operator saw a 200 and got no behaviour at all.
    """
    with get_db() as db:
        db.add(Tag(id=1, name="Heist", category="plot_device"))
        db.commit()

    response = client.post(
        "/admin/api/tags/alias", json={"raw": "x" * 250, "tag_id": 1}
    )

    assert response.status_code == 400
    assert "160" in response.json()["detail"]


def test_an_alias_at_the_limit_still_promotes_and_resolves(client):
    """The boundary must remain usable, not just safe."""
    from app.taxonomy import normalize
    from app.tag_validation import resolve_tag
    from app.taxonomy_sync import load_db_aliases

    with get_db() as db:
        db.add(Tag(id=1, name="Heist", category="plot_device"))
        db.commit()

    raw = "a" * 160
    assert (
        client.post("/admin/api/tags/alias", json={"raw": raw, "tag_id": 1}).status_code
        == 200
    )

    with get_db() as db:
        aliases = load_db_aliases(db)
    assert normalize(raw) in aliases
    tag, how = resolve_tag(raw, db_aliases=aliases)
    assert tag is not None and how == "db_alias"


# ---------------------------------------------------------------------------
# Off-vocabulary paging and scoping
# ---------------------------------------------------------------------------
def test_offvocab_totals_describe_the_filtered_set(client):
    """Two different quantities used to be presented as one.

    The totals counted the whole table regardless of `resolution`, so a
    filtered list of 40 rows sat under a header claiming 263,512. An operator
    reading "how much is left to triage" got the wrong number.
    """
    with get_db() as db:
        _observe(db, "Unmapped One", 100)
        _observe(db, "Unmapped Two", 50)
        row = TagObservation(
            normalized="already",
            media_type="movie",
            raw_name="Already Aliased",
            occurrences=999,
            resolution="aliased",
            sample_tmdb_ids=[],
        )
        db.add(row)
        db.commit()

    body = client.get("/admin/api/tags/offvocab?resolution=unmapped").json()

    assert body["total_observations"] == 2
    assert body["total_occurrences"] == 150, "the aliased row's 999 must not count"


def test_offvocab_pages_through_the_tail(client):
    """Without an offset everything past the top 500 was unreachable."""
    with get_db() as db:
        for i in range(10):
            _observe(db, f"Entry {i:02d}", 100 - i)
        db.commit()

    first = client.get("/admin/api/tags/offvocab?limit=4").json()
    second = client.get("/admin/api/tags/offvocab?limit=4&offset=4").json()

    assert len(first["items"]) == 4
    assert first["has_more"] is True
    assert second["offset"] == 4
    # Disjoint: ties in the tail are common, so ordering needs a deterministic
    # second key or paging silently skips and repeats rows.
    assert not {i["normalized"] for i in first["items"]} & {
        i["normalized"] for i in second["items"]
    }


def test_offvocab_filters_by_media_type(client):
    with get_db() as db:
        _observe(db, "Movie Only", 10, media_type="movie")
        _observe(db, "Show Only", 10, media_type="tv")
        db.commit()

    body = client.get("/admin/api/tags/offvocab?media_type=tv").json()

    assert [i["raw_name"] for i in body["items"]] == ["Show Only"]
    assert body["total_observations"] == 1


def test_offvocab_exact_lookup_for_observation_deep_link(client):
    with get_db() as db:
        _observe(db, "Shared Idea", 4, media_type="movie")
        _observe(db, "Shared Idea", 3, media_type="tv")
        _observe(db, "Another Idea", 20, media_type="movie")
        db.commit()

    exact = client.get(
        "/admin/api/tags/offvocab",
        params={
            "normalized": "shared idea",
            "media_type": "tv",
            "resolution": "all",
            "limit": 1,
        },
    ).json()
    assert exact["total_observations"] == 1
    assert exact["total_occurrences"] == 3
    assert [(row["normalized"], row["media_type"]) for row in exact["items"]] == [
        ("shared idea", "tv")
    ]

    missing = client.get(
        "/admin/api/tags/offvocab",
        params={"normalized": "not here", "media_type": "tv", "resolution": "all"},
    ).json()
    assert missing["total_observations"] == 0
    assert missing["items"] == []


def test_sample_ids_gives_a_costed_validation_set(client):
    """The whole point of scoping: prove the promotion works for cents."""
    with get_db() as db:
        db.add(
            TagObservation(
                normalized="dream logic",
                media_type="movie",
                raw_name="Dream Logic",
                occurrences=4210,
                resolution="aliased",
                sample_tmdb_ids=[603, 604],
            )
        )
        db.add(
            TagObservation(
                normalized="slow burn",
                media_type="tv",
                raw_name="Slow Burn",
                occurrences=90,
                resolution="aliased",
                sample_tmdb_ids=[1396],
            )
        )
        db.commit()

    body = client.get("/admin/api/tags/alias/sample-ids").json()

    assert body["tmdb_ids"] == 3
    assert sorted(body["movie"]) == [603, 604]
    assert body["tv"] == [1396]
    assert body["est_cost_usd"] < 0.01, "a validation pass must be trivially cheap"


def test_sample_ids_defaults_to_what_was_just_aliased(client):
    """Unmapped observations are not what you validate -- you validate the
    promotions you just made."""
    with get_db() as db:
        _observe(db, "Still Unmapped", 500, samples=[999])
        db.commit()

    body = client.get("/admin/api/tags/alias/sample-ids").json()

    assert body["tmdb_ids"] == 0
    assert (
        client.get("/admin/api/tags/alias/sample-ids?resolution=all").json()["tmdb_ids"]
        == 1
    )


def test_a_scoped_rebuild_reports_how_many_ids_it_took(client):
    with get_db() as db:
        db.add(MediaMetadata(tmdb_id=603, media_type="movie", title="The Matrix"))
        db.commit()

    body = client.post(
        "/admin/api/tagging/rebuild/start",
        json={"tmdb_ids": [603], "force": True, "limit": 1},
    ).json()

    assert body["scoped_ids"] == 1


def test_a_malformed_id_list_is_rejected(client):
    for payload in ({"tmdb_ids": "603"}, {"tmdb_ids": ["not-an-id"]}):
        assert (
            client.post("/admin/api/tagging/rebuild/start", json=payload).status_code
            == 400
        )


# ---------------------------------------------------------------------------
# Editorial controls over generated rows
# ---------------------------------------------------------------------------
_SYNTH_KEYS = ("SYNTH_EXCLUDED_TAGS", "SYNTH_EXCLUDED_WORDS", "SYNTH_ROTATION_DAYS")


def _clear_synth_state():
    """Reset both halves of the setting: the row and the live value.

    These are the two-sided kind -- an AdminSetting row that persists in the
    test database file, and a mutation of the process-wide settings object.
    Clearing only the second leaves a row that the next session's startup
    replays, so a test that passes alone fails in a full run for reasons that
    have nothing to do with it. Which is exactly what happened here.
    """
    from app.config import Settings, settings

    with get_db() as db:
        db.query(AdminSetting).filter(AdminSetting.key.in_(_SYNTH_KEYS)).delete(
            synchronize_session=False
        )
        db.commit()
    for attr in ("synth_excluded_tags", "synth_excluded_words", "synth_rotation_days"):
        object.__setattr__(settings, attr, Settings.model_fields[attr].default)


@pytest.fixture()
def restore_synth_settings():
    _clear_synth_state()
    yield
    _clear_synth_state()


def test_synth_config_ships_with_nothing_excluded(client, restore_synth_settings):
    """Which shelves an install wants is the operator's call, not this code's.

    The first version of this shipped three hardcoded exclusions as defaults.
    An install inherits an editorial position it never chose, which is exactly
    backwards. Asserted against the field defaults rather than the live object,
    so it is a claim about what ships and not about process state.
    """
    from app.config import Settings

    assert Settings.model_fields["synth_excluded_tags"].default == ""
    assert Settings.model_fields["synth_excluded_words"].default == ""

    body = client.get("/admin/api/categories/synth/config").json()

    assert body["excluded_tags"] == []
    assert body["excluded_words"] == []
    assert "Neo-Noir" in body["known_tags"], "the picker needs the taxonomy"
    openai = next(
        provider for provider in body["providers"] if provider["id"] == "openai"
    )
    editor_models = {model["id"] for model in openai["models"]}
    assert {"gpt-5.4-mini", "gpt-5.6-terra", "gpt-5.6-sol"} <= editor_models


def test_saving_exclusions_applies_them_without_a_restart(
    client, restore_synth_settings
):
    from app.category_synth import _excluded_tags, _excluded_words

    body = client.post(
        "/admin/api/categories/synth/config",
        json={
            "excluded_tags": "Erotic Thriller, Queer Lead",
            "excluded_words": "erotic, queer",
        },
    ).json()

    assert body["excluded_tags"] == ["Erotic Thriller", "Queer Lead"]
    # The synthesizer reads the live settings object, so this is the assertion
    # that the change is actually in force rather than merely stored.
    assert _excluded_tags() == {"erotic thriller", "queer lead"}
    assert _excluded_words() == {"erotic", "queer"}


def test_an_excluded_tag_stops_naming_a_row(
    client, restore_synth_settings, monkeypatch
):
    """The narrow scope, asserted: exclusion filters candidates, nothing else."""
    from app import category_synth
    from app.category_synth import _candidate_tags
    from workers.tagging_common import tagger_version

    monkeypatch.setattr(category_synth, "MIN_ROW_SUPPORT", 1)
    monkeypatch.setattr(category_synth, "MAX_DOC_FRACTION", 1.0)

    with get_db() as db:
        db.add(Tag(id=1, name="Neo-Noir", category="subgenre"))
        db.add(TagStats(tag_id=1, media_type="movie", doc_count=500, corpus_size=10000))
        for tmdb_id in range(1, 4):
            db.add(
                MediaMetadata(
                    tmdb_id=tmdb_id,
                    media_type="movie",
                    title="t",
                    overview="A complete synopsis with enough evidence for semantic tagging.",
                    vote_average=8.0,
                    vote_count=5000,
                    release_date="2020-01-01",
                )
            )
            db.add(
                MovieTag(
                    tmdb_id=tmdb_id,
                    tag_id=1,
                    media_type="movie",
                    confidence=0.9,
                    weight_norm=1.0,
                    tagger_version=tagger_version(),
                )
            )
        db.commit()

        assert 1 in _candidate_tags(db, "movie")[0]

    client.post(
        "/admin/api/categories/synth/config", json={"excluded_tags": "Neo-Noir"}
    )

    with get_db() as db:
        assert 1 not in _candidate_tags(db, "movie")[0]


def test_an_exclusion_list_can_be_cleared(client, restore_synth_settings):
    """POST /api/settings skips empty values, so it can set a list but never
    clear one -- and "no exclusions" is the default state. That is why this
    endpoint exists separately."""
    client.post(
        "/admin/api/categories/synth/config", json={"excluded_tags": "Queer Lead"}
    )

    body = client.post(
        "/admin/api/categories/synth/config", json={"excluded_tags": ""}
    ).json()

    assert body["excluded_tags"] == []
    assert (
        client.get("/admin/api/categories/synth/config").json()["excluded_tags"] == []
    )


def test_exclusions_survive_a_restart(client, restore_synth_settings):
    """Stored as AdminSetting rows, which boot replays over the config."""
    from app.admin import load_settings_from_db
    from app.config import settings

    client.post(
        "/admin/api/categories/synth/config",
        json={"excluded_tags": "Queer Lead", "rotation_days": 14},
    )

    object.__setattr__(settings, "synth_excluded_tags", "")
    object.__setattr__(settings, "synth_rotation_days", 7)
    load_settings_from_db()

    assert settings.synth_excluded_tags == "Queer Lead"
    assert settings.synth_rotation_days == 14


def test_a_tag_name_that_is_not_in_the_taxonomy_is_reported_not_rejected(
    client, restore_synth_settings
):
    """A misspelling excludes nothing and would otherwise be silent.

    Not a hard error: an operator may legitimately be excluding a tag from a
    taxonomy version they have not deployed yet.
    """
    body = client.post(
        "/admin/api/categories/synth/config",
        json={"excluded_tags": "Neo-Noir, Nno-Noir"},
    ).json()

    assert body["unknown_tags"] == ["Nno-Noir"]
    assert body["excluded_tags"] == ["Neo-Noir", "Nno-Noir"]


def test_rotation_period_round_trips_and_rejects_garbage(
    client, restore_synth_settings
):
    from app.config import settings

    assert (
        client.post(
            "/admin/api/categories/synth/config", json={"rotation_days": 0}
        ).json()["rotation_days"]
        == 0
    ), "0 must be reachable -- it is how an operator turns rotation off"
    assert settings.synth_rotation_days == 0

    assert (
        client.post(
            "/admin/api/categories/synth/config", json={"rotation_days": "weekly"}
        ).status_code
        == 400
    )
