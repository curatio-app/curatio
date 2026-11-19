"""Curatio's initial tag pack: export, import and download.

A tag pack lets a new install skip paying to tag common titles. It holds only
Curatio's own tags, keyed by TMDB id (plus the IMDb id for matching):

    {"curatio_tag_pack": 1, "version": "...", "tagger_version": "...",
     "taxonomy_version": "5.1.0", "created_at": "...", "titles": 31234,
     "license": "CC0-1.0"}                                    <- first line
    {"tmdb_id": 949, "imdb_id": "tt0113277", "media_type": "movie",
     "tags": [["Heist", 0.93], ["Slow-Burn", 0.71]]}          <- one per title

gzipped JSONL. No titles, overviews, keywords, artwork, credits or ratings:
everything else each install fetches with its owner's own keys.

Precedence on import: a title's pack tags replace rule-derived tags or older
pack tags, but never tags an install made with its own AI model.

Installs download the pack whole from a static manifest URL
(``TAG_PACK_URL``), so nobody learns what is in anyone's library.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import re
import tempfile
from typing import IO, Any, Iterable, Iterator

from loguru import logger
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models import AdminSetting, MediaMetadata, MovieTag, Tag
from app.time_utils import utc_now

FORMAT = 1
LICENSE = "CC0-1.0"
#: Tag sources written by an AI model on an install.
AI_SOURCES = ("gemini", "anthropic", "openai", "local")
#: Hard limits for importing a file someone handed you.
MAX_TITLES = 500_000
MAX_TAGS_PER_TITLE = 60
MAX_LINE_BYTES = 16_384

_VERSION_KEY = "tag_pack_version"  # lower-case: read directly, never applied
_IMDB = re.compile(r"^tt\d{1,10}$")
_HEADER_KEYS = {
    "curatio_tag_pack",
    "version",
    "tagger_version",
    "taxonomy_version",
    "created_at",
    "titles",
    "license",
}
_ITEM_KEYS = {"tmdb_id", "imdb_id", "media_type", "tags"}


class TagPackError(ValueError):
    """The file is not a valid Curatio tag pack."""


# --- export -------------------------------------------------------------------
def _pack_titles(
    db: Session, min_votes: int
) -> Iterator[tuple[int, str, str | None, list[list]]]:
    """(tmdb_id, media_type, imdb_id, [[tag, confidence], ...]) for every title
    with at least one AI-made tag and enough TMDB votes.

    Rows with no ``source`` predate that column and were all made by the AI
    tagger, so they count as AI-made. Only tags still in the active
    vocabulary are exported.
    """
    names = {
        t.id: t.name for t in db.query(Tag.id, Tag.name).filter(Tag.is_active.is_(True))
    }
    for media_type in ("movie", "tv"):
        ai_titles = (
            db.query(MovieTag.tmdb_id)
            .filter(
                MovieTag.media_type == media_type,
                or_(MovieTag.source.in_(AI_SOURCES), MovieTag.source.is_(None)),
            )
            .distinct()
            .subquery()
        )
        meta = (
            db.query(MediaMetadata.tmdb_id, MediaMetadata.imdb_id)
            .filter(
                MediaMetadata.media_type == media_type,
                MediaMetadata.tmdb_id.in_(db.query(ai_titles.c.tmdb_id)),
                MediaMetadata.vote_count >= min_votes,
                MediaMetadata.adult.isnot(True),
            )
            .order_by(MediaMetadata.tmdb_id)
        )
        ids = [(int(t), i) for t, i in meta]
        for start in range(0, len(ids), 2000):
            chunk = ids[start : start + 2000]
            tags: dict[int, list[list]] = {}
            for tmdb_id, tag_id, confidence in (
                db.query(MovieTag.tmdb_id, MovieTag.tag_id, MovieTag.confidence)
                .filter(
                    MovieTag.media_type == media_type,
                    MovieTag.tmdb_id.in_([t for t, _ in chunk]),
                )
                .order_by(MovieTag.tmdb_id, MovieTag.confidence.desc())
            ):
                name = names.get(tag_id)
                if name:
                    tags.setdefault(int(tmdb_id), []).append(
                        [name, round(float(confidence or 0.0), 3)]
                    )
            for tmdb_id, imdb_id in chunk:
                if tags.get(tmdb_id):
                    yield tmdb_id, media_type, imdb_id, tags[tmdb_id]


def export_pack(db: Session, min_votes: int = 50) -> tuple[str, dict[str, Any]]:
    """Write a pack to a temporary file. Returns (path, manifest)."""
    from app.taxonomy import TAXONOMY_VERSION
    from workers.tagging_common import tagger_version

    rows = list(_pack_titles(db, min_votes))
    created = utc_now().replace(microsecond=0)
    header = {
        "curatio_tag_pack": FORMAT,
        "version": created.strftime("%Y.%m.%d.%H%M"),
        "tagger_version": tagger_version(),
        "taxonomy_version": TAXONOMY_VERSION,
        "created_at": created.isoformat() + "Z",
        "titles": len(rows),
        "license": LICENSE,
    }
    fd, path = tempfile.mkstemp(prefix="curatio-tag-pack-", suffix=".jsonl.gz")
    os.close(fd)
    with gzip.open(path, "wt", encoding="utf-8") as out:
        out.write(json.dumps(header, separators=(",", ":")) + "\n")
        for tmdb_id, media_type, imdb_id, tags in rows:
            item: dict[str, Any] = {"tmdb_id": tmdb_id, "media_type": media_type}
            if imdb_id and _IMDB.match(imdb_id):
                item["imdb_id"] = imdb_id
            item["tags"] = tags
            out.write(json.dumps(item, separators=(",", ":")) + "\n")
    digest = _sha256(path)
    manifest = {**header, "sha256": digest, "file": os.path.basename(path)}
    logger.info("Exported tag pack: {} titles, sha256 {}", len(rows), digest[:12])
    return path, manifest


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


# --- reading ------------------------------------------------------------------
def _lines(stream: IO[bytes]) -> Iterator[str]:
    with gzip.open(stream, "rt", encoding="utf-8") as text:
        for raw in text:
            line = str(raw)
            if len(line) > MAX_LINE_BYTES:
                raise TagPackError("A line is longer than any valid pack line")
            line = line.strip()
            if line:
                yield line


def _check_header(raw: str) -> dict[str, Any]:
    try:
        header = json.loads(raw)
    except ValueError as exc:
        raise TagPackError("The first line is not JSON") from exc
    if not isinstance(header, dict) or header.get("curatio_tag_pack") != FORMAT:
        raise TagPackError("Not a Curatio tag pack (or an unsupported format)")
    extra = set(header) - _HEADER_KEYS
    if extra:
        raise TagPackError(f"Unexpected header fields: {sorted(extra)}")
    for key in ("version", "tagger_version", "taxonomy_version"):
        if not isinstance(header.get(key), str) or len(header[key]) > 64:
            raise TagPackError(f"Header field {key} is missing or invalid")
    return header


def _check_item(raw: str) -> tuple[int, str, list[tuple[str, float]]]:
    try:
        item = json.loads(raw)
    except ValueError as exc:
        raise TagPackError("A title line is not JSON") from exc
    if not isinstance(item, dict) or set(item) - _ITEM_KEYS:
        # Anything beyond ids and tags must never ride along in a pack.
        raise TagPackError("A title line carries fields other than ids and tags")
    tmdb_id = item.get("tmdb_id")
    media_type = item.get("media_type")
    imdb_id = item.get("imdb_id")
    tags = item.get("tags")
    if not isinstance(tmdb_id, int) or isinstance(tmdb_id, bool) or tmdb_id <= 0:
        raise TagPackError("Invalid tmdb_id")
    if media_type not in ("movie", "tv"):
        raise TagPackError("Invalid media_type")
    if imdb_id is not None and not (isinstance(imdb_id, str) and _IMDB.match(imdb_id)):
        raise TagPackError("Invalid imdb_id")
    if not isinstance(tags, list) or not 0 < len(tags) <= MAX_TAGS_PER_TITLE:
        raise TagPackError("Invalid tags list")
    parsed = []
    for entry in tags:
        if (
            not isinstance(entry, list)
            or len(entry) != 2
            or not isinstance(entry[0], str)
            or len(entry[0]) > 100
            or not isinstance(entry[1], (int, float))
            or isinstance(entry[1], bool)
            or not 0 <= entry[1] <= 1
        ):
            raise TagPackError("Invalid tag entry")
        parsed.append((entry[0], float(entry[1])))
    return tmdb_id, media_type, parsed


def read_pack(stream: IO[bytes]) -> tuple[dict[str, Any], list]:
    """Validate a whole pack. Raises :class:`TagPackError` on anything odd."""
    lines = _lines(stream)
    try:
        header = _check_header(next(lines))
    except StopIteration as exc:
        raise TagPackError("The file is empty") from exc
    except (OSError, EOFError) as exc:
        raise TagPackError("Not a gzip file") from exc
    items = []
    try:
        for raw in lines:
            items.append(_check_item(raw))
            if len(items) > MAX_TITLES:
                raise TagPackError("Too many titles")
    except (OSError, EOFError) as exc:
        raise TagPackError("The gzip stream is damaged") from exc
    return header, items


# --- import -------------------------------------------------------------------
def _ai_tagged(db: Session, media_type: str, ids: list[int]) -> set[int]:
    found: set[int] = set()
    for start in range(0, len(ids), 1000):
        chunk = ids[start : start + 1000]
        found.update(
            int(row[0])
            for row in db.query(MovieTag.tmdb_id)
            .filter(
                MovieTag.media_type == media_type,
                MovieTag.tmdb_id.in_(chunk),
                MovieTag.source.in_(AI_SOURCES),
            )
            .distinct()
        )
    return found


def import_pack(
    db: Session, stream: IO[bytes], *, expected_sha256: str | None = None
) -> dict[str, Any]:
    """Validate and store a pack. Returns counts."""
    from workers.tagging_common import upsert_movie_tags

    data = stream.read()
    if expected_sha256 and hashlib.sha256(data).hexdigest() != expected_sha256:
        raise TagPackError("Checksum mismatch: the download is incomplete or altered")
    header, items = read_pack(io.BytesIO(data))
    tag_ids = {name: tid for tid, name in db.query(Tag.id, Tag.name)}
    version = header["tagger_version"][:20]
    now = utc_now()

    by_type: dict[str, list] = {"movie": [], "tv": []}
    for tmdb_id, media_type, tags in items:
        by_type[media_type].append((tmdb_id, tags))

    stored = kept = unknown = 0
    for media_type, entries in by_type.items():
        keep = _ai_tagged(db, media_type, [t for t, _ in entries])
        for start in range(0, len(entries), 500):
            chunk = entries[start : start + 500]
            replace = [t for t, _ in chunk if t not in keep]
            kept += len(chunk) - len(replace)
            if not replace:
                continue
            db.query(MovieTag).filter(
                MovieTag.media_type == media_type,
                MovieTag.tmdb_id.in_(replace),
            ).delete(synchronize_session=False)
            rows = []
            wanted = set(replace)
            for tmdb_id, tags in chunk:
                if tmdb_id not in wanted:
                    continue
                for name, confidence in tags:
                    tag_id = tag_ids.get(name)
                    if tag_id is None:
                        unknown += 1
                        continue
                    rows.append(
                        {
                            "tmdb_id": tmdb_id,
                            "media_type": media_type,
                            "tag_id": tag_id,
                            "confidence": confidence,
                            "weight": None,
                            "weight_norm": None,
                            "tagged_at": now,
                            "tagger_version": version,
                            "source": "pack",
                            "model": None,
                        }
                    )
                stored += 1
            upsert_movie_tags(db, rows)
        db.commit()

    _remember_version(db, header["version"])
    result = {
        "version": header["version"],
        "titles": len(items),
        "stored": stored,
        "kept_local_ai": kept,
        "unknown_tags": unknown,
        "taxonomy_version": header["taxonomy_version"],
    }
    logger.info("Imported tag pack: {}", result)
    return result


def _remember_version(db: Session, version: str) -> None:
    row = db.get(AdminSetting, _VERSION_KEY)
    if row is None:
        db.add(AdminSetting(key=_VERSION_KEY, value=version))
    else:
        row.value = version
    db.commit()


def installed_version(db: Session) -> str | None:
    row = db.get(AdminSetting, _VERSION_KEY)
    return row.value if row and row.value else None


def pack_title_count(db: Session) -> int:
    from sqlalchemy import func

    return int(
        db.query(func.count(func.distinct(MovieTag.tmdb_id)))
        .filter(MovieTag.source == "pack")
        .scalar()
        or 0
    )


# --- download -----------------------------------------------------------------
async def update_from_url(force: bool = False) -> dict[str, Any]:
    """Download and import the pack named by ``TAG_PACK_URL`` if it is newer.

    The manifest is ``{"version": ..., "url": ..., "sha256": ...}``; ``url``
    may be relative to the manifest.
    """
    import asyncio
    from urllib.parse import urljoin

    from app.config import settings
    from app.database import get_db
    from app.sources.sdk import http_client

    manifest_url = (settings.tag_pack_url or "").strip()
    if not manifest_url:
        return {"status": "not_configured"}
    resp = await http_client().get(manifest_url, timeout=20, follow_redirects=True)
    resp.raise_for_status()
    manifest = resp.json()
    version = str(manifest.get("version") or "")
    if not version or not manifest.get("url") or not manifest.get("sha256"):
        raise TagPackError("The manifest needs version, url and sha256")
    with get_db() as db:
        current = installed_version(db)
    if current == version and not force:
        return {"status": "current", "version": version}
    pack = await http_client().get(
        urljoin(manifest_url, str(manifest["url"])),
        timeout=300,
        follow_redirects=True,
    )
    pack.raise_for_status()

    def run() -> dict[str, Any]:
        with get_db() as db:
            return import_pack(
                db, io.BytesIO(pack.content), expected_sha256=str(manifest["sha256"])
            )

    result = await asyncio.to_thread(run)
    await asyncio.to_thread(_after_import)
    return {"status": "imported", **result}


def _after_import() -> None:
    """Recompute tag weights and rebuild Home with the new tags."""
    try:
        from workers.daily_update import refresh_relevance_threaded

        refresh_relevance_threaded("tag_pack")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Relevance refresh after tag pack import failed: {}", exc)
    try:
        from app import library_catalog

        library_catalog.invalidate_home()
    except Exception as exc:  # noqa: BLE001
        logger.debug("Home invalidation after tag pack import skipped: {}", exc)


def iter_lines(path: str) -> Iterable[str]:  # pragma: no cover - debugging aid
    with gzip.open(path, "rt", encoding="utf-8") as text:
        yield from text
