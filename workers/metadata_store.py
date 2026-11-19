"""Writing TMDB metadata without destroying what enrichment already learned.

``TMDBClient.extract_metadata`` always returns the full column set, filling in
``None`` for anything the payload does not carry. That is fine for the *detail*
payloads (``/movie/{id}``, ``/tv/{id}``) the enricher fetches, which carry
everything. It is actively destructive for the *list* payloads that acquisition
and the daily update work with -- ``/discover``, ``/popular``, ``/top_rated``
results carry no ``external_ids``, no ``images``, no ``release_dates`` and no
season counts, so ``extract_metadata`` returns ``None`` for:

    imdb_id, logo_path, digital_release_date,
    next_episode_air_date, number_of_seasons, number_of_episodes

and a much smaller ``raw_data``.

``db.merge(MediaMetadata(**metadata_dict))`` sets every one of those attributes
explicitly, ``None`` included, so merging a list payload over an enriched row
**erases** those columns. ``imdb_id`` in particular is load-bearing: MDBList
matching, the fingerprint layer and stream resolution all key off it, and it is
only ever populated by a detail call.

So every write of a list-shaped payload goes through :func:`upsert_media_metadata`,
which updates only the keys that actually carry a value. A detail payload loses
nothing by taking the same path -- it simply has no ``None``s to skip.

The contract is stated in ``extract_metadata``'s own docstring ("the callers'
selective non-null updates then leave any previously stored detail-level date
intact rather than clobbering it"). It was honoured in exactly one of four
callers before this module existed.
"""

from __future__ import annotations

from typing import Any, Iterable

from sqlalchemy.orm import Session

from app.models import MediaMetadata

#: Keys only a TMDB *detail* response carries. A list payload has none of them,
#: so their presence is a reliable "this is the richer document" signal.
_DETAIL_MARKERS = ("credits", "seasons", "external_ids", "keywords", "release_dates")

# List/discovery responses can carry stale or alternate dates (the pilot had
# The Godfather as 2022, Fight Club as 2000 and The Avengers as 2024). Once a
# detail document has established canonical identity fields, only another
# detail document may replace them. Volatile ranking fields such as popularity,
# votes and artwork still update from list responses.
_DETAIL_OWNED_FIELDS = frozenset({"title", "original_title", "release_date"})


def _is_detail_payload(raw: Any) -> bool:
    return isinstance(raw, dict) and any(k in raw for k in _DETAIL_MARKERS)


def upsert_media_metadata(db: Session, metadata: dict[str, Any]) -> bool:
    """Insert or non-destructively update one ``media_metadata`` row.

    Returns True if a new row was inserted, False if an existing one was
    updated. Does not commit -- callers batch their own commits.
    """
    tmdb_id = metadata.get("tmdb_id")
    media_type = metadata.get("media_type")
    if tmdb_id is None or media_type is None:
        # Nothing to key on. Silently skipping would hide a payload-shape
        # change, which is the failure mode this whole module exists to stop.
        raise ValueError(
            f"metadata needs tmdb_id and media_type, got {tmdb_id!r}/{media_type!r}"
        )

    existing = (
        db.query(MediaMetadata)
        .filter(
            MediaMetadata.tmdb_id == tmdb_id,
            MediaMetadata.media_type == media_type,
        )
        .first()
    )
    if existing is None:
        db.add(MediaMetadata(**metadata))
        return True

    existing_has_detail = _is_detail_payload(existing.raw_data)
    incoming_has_detail = _is_detail_payload(metadata.get("raw_data"))
    for key, value in metadata.items():
        if value is None:
            continue
        if (
            key in _DETAIL_OWNED_FIELDS
            and existing_has_detail
            and not incoming_has_detail
        ):
            continue
        # raw_data needs more than a None check: a list payload is not absent,
        # it is a *smaller dict*, so the rule above would happily overwrite a
        # pruned detail document with an id-and-title stub. The row would then
        # still carry enrich_version -- claiming to be enriched -- while
        # fingerprint_worker found no `seasons` in it.
        if key == "raw_data" and existing_has_detail and not _is_detail_payload(value):
            continue
        setattr(existing, key, value)
    return False


def upsert_many(
    db: Session, rows: Iterable[dict[str, Any]], *, batch_size: int = 500
) -> dict[str, int]:
    """Upsert a sequence of metadata dicts, committing every ``batch_size``.

    Commits in batches rather than once at the end so an interrupted
    acquisition keeps what it already wrote -- the same resumability property
    every other stage of the pipeline has.
    """
    stats = {"inserted": 0, "updated": 0}
    pending = 0
    for row in rows:
        if upsert_media_metadata(db, row):
            stats["inserted"] += 1
        else:
            stats["updated"] += 1
        pending += 1
        if pending >= batch_size:
            db.commit()
            pending = 0
    if pending:
        db.commit()
    return stats
