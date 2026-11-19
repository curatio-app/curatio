"""
Reconcile the database ``tags`` table with :mod:`app.taxonomy`.

Called from ``init_database()`` on every startup, immediately before
``seed_categories``. That placement fixes a bug on its own: before taxonomy 2.0
the only thing that created ``Tag`` rows was ``create_tags()`` inside
``workers/initial_build.py``, so a fresh install had an empty ``tags`` table
until somebody ran a full build.

The contract is idempotent and strictly non-destructive:

* insert tags that are missing
* update facet/gloss/flags in place for tags that exist
* resolve ``parent_id`` in a second pass, once every row has an id
* reconcile taxonomy-owned aliases while preserving administrator aliases
* a tag dropped from the taxonomy is marked ``is_active=False`` -- **never**
  deleted, and its ``movie_tags`` assignments are never touched

That last rule is R1. ``movie_tags`` rows reference ``tags.id``; deleting a tag
would cascade and destroy assignments that took hours of Gemini time to produce.
"""

from __future__ import annotations


from loguru import logger
from sqlalchemy.orm import Session

from app.time_utils import utc_now  # noqa: E402
from app.models import Tag, TagAlias, TagObservation
from app.taxonomy import TAXONOMY, TAXONOMY_VERSION, normalize


def sync_taxonomy(db: Session) -> dict[str, int]:
    """Bring ``tags`` and ``tag_aliases`` in line with the taxonomy.

    Returns:
        Counts for the admin log: ``created``, ``updated``, ``deactivated``,
        ``reactivated``, ``aliases``, ``aliases_removed``.
    """
    existing: dict[str, Tag] = {str(row.name): row for row in db.query(Tag).all()}
    counts = {
        "created": 0,
        "updated": 0,
        "deactivated": 0,
        "reactivated": 0,
        "aliases": 0,
        "aliases_removed": 0,
    }

    # --- pass 1: insert / update ---
    for tag_def in TAXONOMY:
        row = existing.get(tag_def.name)
        if row is None:
            db.add(
                Tag(
                    name=tag_def.name,
                    category=tag_def.facet,
                    description=tag_def.gloss,
                    display=tag_def.display,
                    is_active=True,
                    is_derived=tag_def.derived,
                    maturity_level=tag_def.maturity,
                    sort_hint=tag_def.sort_hint,
                    taxonomy_version=TAXONOMY_VERSION,
                    created_at=utc_now(),
                )
            )
            counts["created"] += 1
            continue

        changed = False
        for attr, value in (
            ("category", tag_def.facet),
            ("description", tag_def.gloss),
            ("display", tag_def.display),
            ("is_derived", tag_def.derived),
            ("maturity_level", tag_def.maturity),
            ("sort_hint", tag_def.sort_hint),
            ("taxonomy_version", TAXONOMY_VERSION),
        ):
            if getattr(row, attr) != value:
                setattr(row, attr, value)
                changed = True
        if not row.is_active:
            row.is_active = True
            counts["reactivated"] += 1
            changed = True
        if changed:
            counts["updated"] += 1

    db.flush()

    # --- pass 2: parents, now that every row has an id ---
    by_name: dict[str, Tag] = {str(row.name): row for row in db.query(Tag).all()}
    for tag_def in TAXONOMY:
        row = by_name.get(tag_def.name)
        if row is None:
            continue
        parent = by_name.get(tag_def.parent) if tag_def.parent else None
        parent_id = parent.id if parent is not None else None
        if row.parent_id != parent_id:
            row.parent_id = parent_id

    # --- pass 3: deactivate tags no longer in the taxonomy ---
    # Never delete: movie_tags rows reference tags.id with ON DELETE CASCADE.
    taxonomy_names = {t.name for t in TAXONOMY}
    for name, row in by_name.items():
        if name not in taxonomy_names and row.is_active:
            row.is_active = False
            counts["deactivated"] += 1

    # --- pass 4: aliases ---
    # A canonical name always wins, so an alias may never shadow one.
    canonical_keys = {normalize(n) for n in taxonomy_names}
    desired_aliases: dict[str, tuple[int, str]] = {}
    for tag_def in TAXONOMY:
        row = by_name.get(tag_def.name)
        if row is None:
            continue
        for alias in tag_def.aliases:
            key = normalize(alias)
            if not key or key in canonical_keys:
                continue
            desired_aliases.setdefault(key, (int(row.id), alias))

    # Taxonomy aliases are owned by this code and may be retired safely.
    # Administrator-created aliases are user data and are never removed.
    for alias_row in db.query(TagAlias).filter(TagAlias.source == "taxonomy").all():
        desired = desired_aliases.get(str(alias_row.normalized))
        if desired is None or int(alias_row.tag_id) != desired[0]:
            db.delete(alias_row)
            counts["aliases_removed"] += 1
    db.flush()

    known = {row.normalized for row in db.query(TagAlias.normalized).all()}
    for key, (tag_id, raw) in desired_aliases.items():
        if key in known:
            continue
        db.add(
            TagAlias(
                normalized=key,
                tag_id=tag_id,
                raw=raw,
                source="taxonomy",
                created_at=utc_now(),
            )
        )
        known.add(key)
        counts["aliases"] += 1

    db.flush()

    # Older workers recorded every ``extra`` string as unresolved even when it
    # was already a canonical name or a seeded alias. Reclassify those rows on
    # startup so the QA screen is a taxonomy-gap queue, not a misleading dump
    # of known concepts. This touches only matching keys, not the large novel
    # tail, and costs no model calls.
    resolution_map = observation_resolution_map(db)
    if resolution_map:
        observations = (
            db.query(TagObservation)
            .filter(
                TagObservation.resolution == "unmapped",
                TagObservation.normalized.in_(list(resolution_map)),
            )
            .all()
        )
        for observation in observations:
            observation.resolution, observation.mapped_tag_id = resolution_map[
                observation.normalized
            ]

    db.commit()

    if any(counts.values()):
        logger.info(
            "Taxonomy {}: {created} created, {updated} updated, "
            "{deactivated} deactivated, {reactivated} reactivated, "
            "{aliases} aliases added, {aliases_removed} aliases removed".format(
                TAXONOMY_VERSION, **counts
            )
        )
    else:
        logger.info(f"Taxonomy {TAXONOMY_VERSION} already up to date")

    return counts


def observation_resolution_map(db: Session) -> dict[str, tuple[str, int]]:
    """Known observation keys mapped to ``(resolution, tag_id)``.

    Canonical names take precedence over aliases, matching validation.
    """
    canonical = {
        normalize(str(name)): int(tag_id)
        for tag_id, name in db.query(Tag.id, Tag.name)
        .filter(Tag.is_active.is_(True))
        .all()
        if normalize(str(name))
    }
    aliases = {
        str(key): int(tag_id)
        for key, tag_id in db.query(TagAlias.normalized, TagAlias.tag_id)
        .join(Tag, Tag.id == TagAlias.tag_id)
        .filter(Tag.is_active.is_(True))
        .all()
    }
    mapped = {key: ("aliased", tag_id) for key, tag_id in aliases.items()}
    mapped.update({key: ("canonical", tag_id) for key, tag_id in canonical.items()})
    return mapped


def load_db_aliases(db: Session) -> dict[str, str]:
    """Return ``{normalized: canonical_tag_name}`` for the validation layer."""
    rows = (
        db.query(TagAlias.normalized, Tag.name)
        .join(Tag, Tag.id == TagAlias.tag_id)
        .filter(Tag.is_active.is_(True))
        .all()
    )
    return {normalized: name for normalized, name in rows}
