"""
Tests for sync_taxonomy() -- the DB reconciliation half of the taxonomy.

The non-destructive guarantee is the one that matters. ``movie_tags`` rows
reference ``tags.id`` with ON DELETE CASCADE, so deleting a tag that fell out of
the taxonomy would silently destroy assignments. Removal must deactivate, never
delete, and a rename must keep the row reachable.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_taxonomy_sync.db")
os.environ.setdefault("TMDB_API_KEY", "test")
os.environ.setdefault("GEMINI_API_KEY", "test")
os.environ.setdefault("MASTER_PASSWORD", "test")
os.environ.setdefault("BASE_URL", "http://localhost:8000")
os.environ.setdefault("SECRET_KEY", "t" * 40)
os.environ.setdefault("SKIP_API_VALIDATION", "true")

from app import taxonomy as tx  # noqa: E402
from app.models import Base, MovieTag, Tag, TagAlias, TagObservation  # noqa: E402
from app.taxonomy_sync import load_db_aliases, sync_taxonomy  # noqa: E402

TEST_DATABASE_URL = "sqlite:///./test_taxonomy_sync.db"
engine = create_engine(TEST_DATABASE_URL, connect_args={"check_same_thread": False})
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture()
def db():
    Base.metadata.create_all(bind=engine)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


# ---------------------------------------------------------------------------
# Fresh install
# ---------------------------------------------------------------------------
def test_seeds_the_whole_taxonomy_on_an_empty_database(db):
    """Fixes the empty-tags bug: before this ran from init_database(), a fresh
    install had no tags at all until somebody ran a full build."""
    counts = sync_taxonomy(db)
    assert counts["created"] == len(tx.TAXONOMY)
    assert db.query(Tag).count() == len(tx.TAXONOMY)


def test_seeded_rows_carry_taxonomy_metadata(db):
    sync_taxonomy(db)
    row = db.query(Tag).filter(Tag.name == "Slasher").one()
    definition = tx.BY_NAME["Slasher"]
    assert row.category == definition.facet
    assert row.description == definition.gloss
    assert row.maturity_level == definition.maturity
    assert row.is_derived is False
    assert row.is_active is True
    assert row.taxonomy_version == tx.TAXONOMY_VERSION


def test_derived_flag_is_persisted(db):
    sync_taxonomy(db)
    assert db.query(Tag).filter(Tag.name == "1990s").one().is_derived is True
    assert db.query(Tag).filter(Tag.name == "Heist").one().is_derived is False


def test_parents_are_resolved_to_ids(db):
    sync_taxonomy(db)
    child = db.query(Tag).filter(Tag.name == "Slasher").one()
    parent = db.query(Tag).filter(Tag.name == "Horror").one()
    assert child.parent_id == parent.id


def test_roots_have_no_parent(db):
    sync_taxonomy(db)
    assert db.query(Tag).filter(Tag.name == "Horror").one().parent_id is None


def test_aliases_are_seeded(db):
    sync_taxonomy(db)
    aliases = load_db_aliases(db)
    assert aliases[tx.normalize("science fiction")] == "Sci-Fi"


def test_aliases_never_shadow_a_canonical_name(db):
    sync_taxonomy(db)
    canonical = {tx.normalize(t.name) for t in tx.TAXONOMY}
    shadowing = [
        a.normalized for a in db.query(TagAlias).all() if a.normalized in canonical
    ]
    assert not shadowing, shadowing


def test_sync_reclassifies_known_observations_out_of_the_unmapped_queue(db):
    db.add_all(
        [
            TagObservation(
                normalized=tx.normalize("Sci-Fi"),
                media_type="movie",
                raw_name="Sci-Fi",
                occurrences=20,
                resolution="unmapped",
            ),
            TagObservation(
                normalized=tx.normalize("science fiction"),
                media_type="tv",
                raw_name="science fiction",
                occurrences=10,
                resolution="unmapped",
            ),
        ]
    )
    db.commit()

    sync_taxonomy(db)

    rows = {row.raw_name: row for row in db.query(TagObservation).all()}
    assert rows["Sci-Fi"].resolution == "canonical"
    assert rows["science fiction"].resolution == "aliased"
    assert rows["Sci-Fi"].mapped_tag_id == rows["science fiction"].mapped_tag_id


# ---------------------------------------------------------------------------
# Idempotence
# ---------------------------------------------------------------------------
def test_running_twice_changes_nothing(db):
    first = sync_taxonomy(db)
    second = sync_taxonomy(db)
    assert first["created"] == len(tx.TAXONOMY)
    assert second == {
        "created": 0,
        "updated": 0,
        "deactivated": 0,
        "reactivated": 0,
        "aliases": 0,
        "aliases_removed": 0,
    }
    assert db.query(Tag).count() == len(tx.TAXONOMY)


def test_running_twice_does_not_duplicate_aliases(db):
    sync_taxonomy(db)
    before = db.query(TagAlias).count()
    sync_taxonomy(db)
    assert db.query(TagAlias).count() == before


def test_sync_removes_only_stale_taxonomy_owned_aliases(db):
    sync_taxonomy(db)
    lighthearted = db.query(Tag).filter(Tag.name == "Lighthearted").one()
    db.add_all(
        [
            TagAlias(
                normalized="obsolete taxonomy alias",
                tag_id=lighthearted.id,
                raw="obsolete taxonomy alias",
                source="taxonomy",
            ),
            TagAlias(
                normalized="admin curated alias",
                tag_id=lighthearted.id,
                raw="admin curated alias",
                source="admin",
            ),
        ]
    )
    db.commit()

    counts = sync_taxonomy(db)

    aliases = {row.normalized: row.source for row in db.query(TagAlias).all()}
    assert counts["aliases_removed"] == 1
    assert "obsolete taxonomy alias" not in aliases
    assert aliases["admin curated alias"] == "admin"


def test_ids_are_stable_across_runs(db):
    """A changed tag id would repoint every movie_tags row."""
    sync_taxonomy(db)
    before = {t.name: t.id for t in db.query(Tag).all()}
    sync_taxonomy(db)
    after = {t.name: t.id for t in db.query(Tag).all()}
    assert before == after


# ---------------------------------------------------------------------------
# Upgrade path from the pre-2.0 vocabulary
# ---------------------------------------------------------------------------
def _seed_legacy(db):
    """Recreate the pre-2.0 tags table shape, with an assignment attached."""
    from tests.test_taxonomy import LEGACY_TAG_NAMES

    legacy_facets = {"Action": "genre", "Dark": "mood", "1990s": "era"}
    for name in LEGACY_TAG_NAMES:
        db.add(Tag(name=name, category=legacy_facets.get(name, "genre")))
    db.flush()
    action = db.query(Tag).filter(Tag.name == "Action").one()
    db.add(MovieTag(tmdb_id=603, media_type="movie", tag_id=action.id, confidence=0.9))
    db.commit()
    return action.id


def test_legacy_rows_are_updated_in_place_not_recreated(db):
    """R1: the tag id must survive, or every movie_tags row is orphaned."""
    action_id = _seed_legacy(db)
    sync_taxonomy(db)
    action = db.query(Tag).filter(Tag.name == "Action").one()
    assert action.id == action_id
    assert action.category == tx.BY_NAME["Action"].facet  # 'genre' -> 'subgenre'


def test_legacy_assignments_survive(db):
    _seed_legacy(db)
    sync_taxonomy(db)
    assignment = db.query(MovieTag).filter(MovieTag.tmdb_id == 603).one()
    assert assignment.confidence == 0.9
    assert db.query(Tag).filter(Tag.id == assignment.tag_id).one().name == "Action"


def test_every_legacy_name_is_present_and_active_after_sync(db):
    from tests.test_taxonomy import LEGACY_TAG_NAMES

    _seed_legacy(db)
    sync_taxonomy(db)
    rows = {t.name: t for t in db.query(Tag).all()}
    for name in LEGACY_TAG_NAMES:
        assert name in rows, name
        assert rows[name].is_active is True, name


def test_legacy_upgrade_creates_only_the_new_tags(db):
    _seed_legacy(db)
    counts = sync_taxonomy(db)
    assert counts["created"] == len(tx.TAXONOMY) - 75
    assert counts["deactivated"] == 0


# ---------------------------------------------------------------------------
# Removal and rename
# ---------------------------------------------------------------------------
def test_removed_tag_is_deactivated_not_deleted(db):
    """Never delete: movie_tags cascades on tags.id."""
    sync_taxonomy(db)
    orphan = Tag(name="Retired Concept", category="mood", is_active=True)
    db.add(orphan)
    db.flush()
    db.add(MovieTag(tmdb_id=42, media_type="movie", tag_id=orphan.id, confidence=0.8))
    db.commit()

    counts = sync_taxonomy(db)

    assert counts["deactivated"] == 1
    survivor = db.query(Tag).filter(Tag.name == "Retired Concept").one()
    assert survivor.is_active is False
    assert db.query(MovieTag).filter(MovieTag.tmdb_id == 42).count() == 1


def test_a_returning_tag_is_reactivated(db):
    sync_taxonomy(db)
    row = db.query(Tag).filter(Tag.name == "Heist").one()
    row.is_active = False
    db.commit()

    counts = sync_taxonomy(db)

    assert counts["reactivated"] == 1
    assert db.query(Tag).filter(Tag.name == "Heist").one().is_active is True


def test_rename_keeps_the_old_row_and_its_assignments(db, monkeypatch):
    """Renaming a TagDef must not orphan the old row.

    The old name is deactivated rather than deleted, so its movie_tags rows
    survive and an operator can map it forward.
    """
    sync_taxonomy(db)
    heist = db.query(Tag).filter(Tag.name == "Heist").one()
    db.add(MovieTag(tmdb_id=99, media_type="movie", tag_id=heist.id, confidence=0.9))
    db.commit()
    old_id = heist.id

    renamed = tuple(
        tx.TagDef(
            name="Heist Job" if t.name == "Heist" else t.name,
            facet=t.facet,
            gloss=t.gloss,
            aliases=t.aliases,
            parent=t.parent,
            display=t.display,
            maturity=t.maturity,
            media=t.media,
            derived=t.derived,
            sort_hint=t.sort_hint,
        )
        for t in tx.TAXONOMY
    )
    monkeypatch.setattr("app.taxonomy_sync.TAXONOMY", renamed)

    sync_taxonomy(db)

    old = db.query(Tag).filter(Tag.name == "Heist").one()
    assert old.is_active is False
    assert old.id == old_id
    assert db.query(MovieTag).filter(MovieTag.tmdb_id == 99).count() == 1
    assert db.query(Tag).filter(Tag.name == "Heist Job").one().is_active is True


# ---------------------------------------------------------------------------
# Update in place
# ---------------------------------------------------------------------------
def test_facet_and_gloss_changes_are_applied(db):
    sync_taxonomy(db)
    row = db.query(Tag).filter(Tag.name == "Heist").one()
    row.category = "wrong_facet"
    row.description = "stale"
    db.commit()

    counts = sync_taxonomy(db)

    assert counts["updated"] >= 1
    refreshed = db.query(Tag).filter(Tag.name == "Heist").one()
    assert refreshed.category == tx.BY_NAME["Heist"].facet
    assert refreshed.description == tx.BY_NAME["Heist"].gloss


def test_sync_leaves_no_tag_without_a_facet(db):
    sync_taxonomy(db)
    assert not [t.name for t in db.query(Tag).all() if t.category not in tx.FACETS]


def test_active_tag_count_matches_the_taxonomy(db):
    sync_taxonomy(db)
    active = db.query(Tag).filter(Tag.is_active.is_(True)).count()
    assert active == len(tx.TAXONOMY)
