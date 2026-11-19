"""
Probe the live MDBList API and report exactly what it returns.

    python workers/diagnose_mdblist.py

Why this exists: ``api.mdblist.com`` is unreachable from the environment this
client was written in, so the per-title request shape was inferred rather than
verified. The result was a request that never asked for review text and a list
path built from two invented slugs -- both of which failed silently, because the
client is designed to degrade rather than raise. This asks the real API, with
the real key, and prints what comes back.

Costs roughly a dozen requests. Prints structure, not payloads: top-level keys,
types, and short samples, so nothing sensitive or enormous lands in a log.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.database import get_db  # noqa: E402
from app.mdblist_client import MDBListClient, get_api_key  # noqa: E402

#: Values worth trying on the per-title endpoint. The client currently sends
#: none of them, which is why ``mdb_facts`` never carried reviews.
APPEND_CANDIDATES = (
    None,
    "review",
    "reviews",
    "keyword",
    "keyword,review",
    "genres,ratings,review",
)

#: The documented per-title path is ``/{media_provider}/{media_type}/{media_id}/``
#: -- note the trailing slash, which the shipped client omitted.
TITLE_PATH = "/{provider}/{media_type}/{media_id}/"

#: Which spelling the API wants for a series.
MEDIA_TYPES = ("movie", "show", "tv", "series")

#: Path shapes for list items. The shipped client assumes ``/lists/{slug}/items``
#: with a bare slug, which cannot identify a user's list. Superseded for ratings
#: by ``POST /rating/{media_type}/{return_rating}``, which is the documented bulk
#: route and takes a body this probe cannot guess -- see the report at the end.
LIST_PATHS = (
    "/lists/{slug}/items/",
    "/lists/{slug}/items",
    "/lists/{slug}/",
)

#: The Matrix / Breaking Bad -- both certain to exist upstream, so a failure is
#: about the request shape rather than a missing title.
PROBE_MOVIE = 603
PROBE_SHOW = 1396


def _describe(value: Any, depth: int = 0) -> str:
    """One-line shape summary: type, size, and a short sample."""
    if isinstance(value, dict):
        keys = list(value.keys())
        return f"dict({len(keys)}) keys={keys[:12]}"
    if isinstance(value, list):
        if not value:
            return "list(0)"
        first = value[0]
        inner = (
            f"dict keys={list(first.keys())[:10]}"
            if isinstance(first, dict)
            else type(first).__name__
        )
        return f"list({len(value)}) of {inner}"
    if isinstance(value, str):
        sample = value.replace("\n", " ")[:90]
        return f'str({len(value)}) "{sample}"'
    return f"{type(value).__name__} {value!r}"


def _report(label: str, payload: Any) -> None:
    print(f"\n--- {label}")
    if payload is None:
        print("    NO RESPONSE (404, error, or quota) — see the debug log")
        return
    if not isinstance(payload, dict):
        print(f"    {_describe(payload)}")
        return
    for key in sorted(payload.keys()):
        print(f"    {key:<24} {_describe(payload[key])}")

    # The question this whole script exists to answer.
    for candidate in ("reviews", "review", "user_reviews", "critic_reviews"):
        if payload.get(candidate):
            print(f"    >>> REVIEWS PRESENT under '{candidate}'")
            entry = payload[candidate]
            sample = entry[0] if isinstance(entry, list) and entry else entry
            print(
                "        sample: "
                + json.dumps(sample, default=str)[:600].replace("\n", " ")
            )
            break
    else:
        print("    >>> no review field in this response")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--list-slug",
        default=None,
        help="A real list to probe, e.g. 'linaspurinis/top-watched-movies-of-the-week'",
    )
    args = parser.parse_args()

    with get_db() as db:
        key = get_api_key(db)
        if not key:
            print("No MDBList key stored. Add one in the admin Tagging tab first.")
            return 1
        client = MDBListClient(key, db)
        client.load_quota()

    try:
        print("=" * 72)
        print("1. WHO ARE WE — tier and daily limit")
        print("=" * 72)
        info = await client.user_info()
        _report("GET /user", info)
        print(f"\n    quota remaining as tracked locally: {client.remaining}")

        print("\n" + "=" * 72)
        print("2. PER-TITLE SHAPE — which append_to_response returns reviews")
        print("=" * 72)
        base = TITLE_PATH.format(
            provider="tmdb", media_type="movie", media_id=PROBE_MOVIE
        )
        for append in APPEND_CANDIDATES:
            params = {} if append is None else {"append_to_response": append}
            payload = await client._get(base, **params)
            _report(f"GET {base} append={append!r}", payload)

        # The shipped client omitted the trailing slash the docs show. Prove
        # whether that mattered rather than assuming either way.
        no_slash = base.rstrip("/")
        _report(f"GET {no_slash}  (no trailing slash)", await client._get(no_slash))

        print("\n" + "=" * 72)
        print("3. MEDIA TYPE — which spelling does a series want?")
        print("=" * 72)
        for kind in MEDIA_TYPES:
            path = TITLE_PATH.format(
                provider="tmdb", media_type=kind, media_id=PROBE_SHOW
            )
            _report(f"GET {path}", await client._get(path))

        print("\n" + "=" * 72)
        print("4. LIST PATHS — which shape actually resolves")
        print("=" * 72)
        slug = args.list_slug or "top-rated-movies"
        for template in LIST_PATHS:
            path = template.format(slug=slug)
            payload = await client._get(path, limit=3)
            _report(f"GET {path}", payload)
        if not args.list_slug:
            print(
                "\n    NOTE: probed with the invented slug 'top-rated-movies'. "
                "Re-run with --list-slug '<user>/<list-name>' from a real "
                "mdblist.com list URL to test a list that exists."
            )

        print("\n" + "=" * 72)
        print(f"requests used this run: {client._used_today}")
        print("=" * 72)
    finally:
        await client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
