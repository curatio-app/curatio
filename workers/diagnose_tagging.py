"""
Diagnose why tagging is failing.

    docker compose --profile workers run --rm worker python workers/diagnose_tagging.py

Checks, in order, and stops at the first thing that is actually broken:

1. Is a Gemini API key configured?
2. Can we reach the API and list models at all? (key valid, network, quota)
3. Is the configured model in that list?
4. Does a real tagging call against it return usable JSON?
5. Is the tag vocabulary present in the database?

This exists because the failure it is built to catch was invisible for two
months. Google retired ``gemini-2.0-flash`` on 2026-06-01; every call began
returning 404; ``tag_items_batch`` caught the exception per batch, incremented a
counter, and the daily job recorded "completed, 0 processed, 49 failed" with no
reason attached. Nothing in the dashboard said *why*, so nothing looked wrong
enough to investigate.

Model IDs keep being retired -- ``gemini-2.5-flash`` is scheduled for
2026-10-16 -- so this will happen again. Step 2 lists what the key can actually
reach, which turns "tagging is broken" into a one-line answer and a config edit.
"""

import argparse
import asyncio
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.config import settings  # noqa: E402

OK = "  [ OK ]"
BAD = "  [FAIL]"
WARN = "  [WARN]"


def _fail(message: str, *hints: str) -> int:
    print(f"{BAD} {message}")
    for hint in hints:
        print(f"         {hint}")
    return 1


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model", default=None, help="Test this model instead of the configured one"
    )
    parser.add_argument(
        "--cost-probe",
        type=int,
        default=0,
        metavar="N",
        help=(
            "Tag N real titles from your library and report MEASURED token usage "
            "and cost, extrapolated to a full re-tag. Costs a few cents."
        ),
    )
    parser.add_argument(
        "--price-in",
        type=float,
        default=None,
        metavar="USD",
        help="$ per 1M input tokens for your model (check your billing console)",
    )
    parser.add_argument(
        "--price-out",
        type=float,
        default=None,
        metavar="USD",
        help="$ per 1M output tokens for your model",
    )
    parser.add_argument(
        "--compare-models",
        default=None,
        metavar="A,B",
        help=(
            "Tag the same sample with several models and print their tags side "
            "by side, to judge whether a cheaper model is good enough."
        ),
    )
    args = parser.parse_args()
    model_name = args.model or settings.gemini_model

    if args.compare_models:
        names = [m.strip() for m in args.compare_models.split(",") if m.strip()]
        return await compare_models(names, args.cost_probe or 10)

    if args.cost_probe:
        return await cost_probe(
            model_name, args.cost_probe, args.price_in, args.price_out
        )

    print("=" * 72)
    print("TAGGING DIAGNOSTIC")
    print("=" * 72)

    # --- 1. key present -----------------------------------------------------
    print("\n1. API key")
    key = settings.gemini_api_key or ""
    if not key or key == "your_gemini_api_key_here":
        return _fail(
            "GEMINI_API_KEY is missing or still the placeholder.",
            "Set it in .env, then restart.",
        )
    print(f"{OK} configured ({key[:6]}...{key[-4:]}, {len(key)} chars)")
    print(f"       configured model: {model_name}")

    # --- 2. can we reach the API ------------------------------------------
    print("\n2. Reachability and available models")
    try:
        from app.gemini_client import GEMINI_API_ROOT

        available: list[str] = []
        page_token = None
        async with httpx.AsyncClient(timeout=30) as client:
            while True:
                params = {"pageSize": 1000}
                if page_token:
                    params["pageToken"] = page_token
                response = await client.get(
                    f"{GEMINI_API_ROOT}/models",
                    headers={"x-goog-api-key": key},
                    params=params,
                )
                response.raise_for_status()
                payload = response.json()
                available.extend(
                    str(model.get("name") or "").removeprefix("models/")
                    for model in payload.get("models") or []
                    if "generateContent"
                    in (model.get("supportedGenerationMethods") or [])
                )
                page_token = payload.get("nextPageToken")
                if not page_token:
                    break
    except Exception as exc:  # noqa: BLE001 - this is the diagnostic
        return _fail(
            f"Could not list models: {type(exc).__name__}: {exc}",
            "Usually an invalid/revoked key, no network, or a blocked egress path.",
        )

    if not available:
        return _fail("The API answered but offers no content-generation models.")

    print(f"{OK} reachable, {len(available)} usable models")
    flash = sorted(m for m in available if "flash" in m)
    for name in flash[:12]:
        print(f"         - {name}")
    if len(flash) > 12:
        print(f"         ... and {len(flash) - 12} more")

    # --- 3. is the configured model one of them ----------------------------
    print(f"\n3. Configured model: {model_name}")
    if model_name not in available:
        suggestion = next(
            (m for m in available if "flash" in m and "lite" not in m), None
        )
        return _fail(
            f"'{model_name}' is NOT available on this key.",
            "This is the failure mode that stops all tagging silently:",
            "a retired model ID returns 404 on every call.",
            "Set GEMINI_MODEL to one of the models listed above"
            + (f", e.g. {suggestion}" if suggestion else "")
            + ", then restart.",
        )
    print(f"{OK} available")

    # --- 4. a real tagging call -------------------------------------------
    print("\n4. Live tagging call")
    try:
        from app.gemini_client import GeminiTaggingEngine

        engine = GeminiTaggingEngine(model_name=model_name)
        result = await engine.tag_items(
            [
                {
                    "tmdb_id": 603,
                    "media_type": "movie",
                    "title": "The Matrix",
                    "overview": (
                        "A computer hacker learns from mysterious rebels about the "
                        "true nature of his reality and his role in the war against "
                        "its controllers."
                    ),
                    "genres": ["Action", "Science Fiction"],
                    "release_date": "1999-03-30",
                }
            ]
        )
    except Exception as exc:  # noqa: BLE001 - this is the diagnostic
        return _fail(
            f"Tagging call raised: {type(exc).__name__}: {exc}",
            "The model exists but the call failed -- check quota, billing,",
            "or safety blocking on this key.",
        )

    if not result:
        return _fail(
            "The call succeeded but returned no tagged items.",
            "Usually a response the JSON parser could not salvage.",
        )
    tags = result[0].get("tags") or {}
    print(f"{OK} returned {len(tags)} tags for The Matrix")
    for name, confidence in list(tags.items())[:8]:
        print(f"         {name}: {confidence}")

    # --- 5. vocabulary in the database -------------------------------------
    print("\n5. Tag vocabulary in the database")
    try:
        from app.database import get_db
        from app.models import Tag

        with get_db() as db:
            total = db.query(Tag).count()
            active = db.query(Tag).filter(Tag.is_active.is_(True)).count()
    except Exception as exc:  # noqa: BLE001 - this is the diagnostic
        return _fail(f"Could not read the tags table: {type(exc).__name__}: {exc}")

    if not total:
        return _fail(
            "The tags table is empty.",
            "sync_taxonomy() runs at startup -- restart the app, or check its log.",
        )
    print(f"{OK} {total} tags ({active} active)")

    unknown = [name for name in tags if name not in _known_tag_names()]
    if unknown:
        print(
            f"{WARN} {len(unknown)} returned tag(s) are outside the vocabulary "
            f"and would be recorded as off-vocabulary: {unknown[:5]}"
        )

    print("\n" + "=" * 72)
    print("All checks passed. Tagging should work.")
    print("=" * 72)
    return 0


def _known_tag_names() -> set:
    from app.taxonomy import BY_NAME

    return set(BY_NAME)


def _sample_titles(sample_size: int):
    """Real titles from the library, most popular first, that have an overview."""
    from app.database import get_db
    from app.models import MediaMetadata

    with get_db() as db:
        rows = (
            db.query(MediaMetadata)
            .filter(MediaMetadata.overview.isnot(None), MediaMetadata.overview != "")
            .order_by(MediaMetadata.popularity.desc().nullslast())
            .limit(sample_size)
            .all()
        )
        library_total = db.query(MediaMetadata).count()
        items = [
            {
                "tmdb_id": r.tmdb_id,
                "media_type": r.media_type,
                "title": r.title,
                "overview": r.overview or "",
                "genres": r.genres if isinstance(r.genres, list) else [],
                "release_date": r.release_date or "",
            }
            for r in rows
        ]
    return items, library_total


async def compare_models(model_names: list, sample_size: int) -> int:
    """Tag the same titles with several models and show the results side by side.

    Cost is the easy half of choosing a model. The half that actually matters is
    whether a cheaper model produces *usable* tags, and no pricing table can
    answer that -- a cheap re-tag that yields four lazy tags per title is a worse
    buy than an expensive one that yields fifteen good ones.

    Tagging is closed-vocabulary classification against a fixed menu, which is
    near the easiest thing an LLM does, so a small model is likely fine. "Likely"
    is not a reason to spend money, hence this.
    """
    from app.gemini_client import GeminiTaggingEngine

    print("=" * 72)
    print(f"MODEL COMPARISON — {sample_size} real titles")
    print("=" * 72)

    items, _total = _sample_titles(sample_size)
    if not items:
        return _fail("No titles with an overview found to compare.")

    results = {}
    for name in model_names:
        try:
            engine = GeminiTaggingEngine(model_name=name)
            tagged = await engine.tag_items(items)
            usage = engine.last_usage or {}
        except Exception as exc:  # noqa: BLE001 - comparing, not asserting
            print(f"\n{BAD} {name}: {type(exc).__name__}: {exc}")
            continue
        by_id = {t.get("tmdb_id"): (t.get("tags") or {}) for t in tagged}
        counts = [len(v) for v in by_id.values()]
        out_tokens = usage.get("candidates_token_count", 0) + usage.get(
            "thoughts_token_count", 0
        )
        results[name] = {
            "by_id": by_id,
            "avg_tags": sum(counts) / len(counts) if counts else 0.0,
            "covered": len(by_id),
            "in": usage.get("prompt_token_count", 0),
            "out": out_tokens,
        }

    if not results:
        return _fail("No model produced a result.")

    print(f"\n{'model':<26}{'tagged':>8}{'avg tags':>10}{'tok in':>10}{'tok out':>10}")
    for name, r in results.items():
        print(
            f"{name:<26}{r['covered']:>3}/{len(items):<4}{r['avg_tags']:>10.1f}"
            f"{r['in']:>10,}{r['out']:>10,}"
        )

    # Agreement against the first model listed. High overlap on a cheaper model
    # is the signal that it is safe to use for the bulk run.
    baseline = model_names[0]
    if baseline in results and len(results) > 1:
        print(f"\nAgreement with {baseline} (shared tags / its tags):")
        base = results[baseline]["by_id"]
        for name, r in results.items():
            if name == baseline:
                continue
            ratios = []
            for tmdb_id, base_tags in base.items():
                other = set(r["by_id"].get(tmdb_id, {}))
                if base_tags:
                    ratios.append(len(set(base_tags) & other) / len(set(base_tags)))
            if ratios:
                print(f"  {name:<26}{sum(ratios) / len(ratios):>6.0%}")

    print("\nSide-by-side tags for the first 3 titles:")
    for item in items[:3]:
        print(f"\n  {item['title']} ({(item.get('release_date') or '')[:4]})")
        for name, r in results.items():
            tags = r["by_id"].get(item["tmdb_id"], {})
            listed = ", ".join(sorted(tags, key=lambda t: -tags[t])[:10]) or "(none)"
            print(f"    {name:<24} {listed}")

    print("\n" + "=" * 72)
    print("Judge the cheap model on tag count AND whether the tags read as correct.")
    print("=" * 72)
    return 0


async def cost_probe(
    model_name: str,
    sample_size: int,
    price_in: float | None,
    price_out: float | None,
) -> int:
    """Tag real titles and report measured tokens and cost.

    Every cost figure in the tagging plan has been an estimate built on assumed
    token counts -- and, for a while, on the price of a model that had already
    been retired. This replaces the arithmetic with a measurement: real titles
    from your own library, real token counts from the API response, extrapolated
    to a full re-tag. It costs a few cents to run.

    Prices are taken from --price-in/--price-out because published rates move and
    differ by tier; read them off your billing console rather than trusting a
    number baked into this file.
    """
    from app.gemini_client import GeminiTaggingEngine

    print("=" * 72)
    print(f"COST PROBE — {sample_size} real titles on {model_name}")
    print("=" * 72)

    items, library_total = _sample_titles(sample_size)

    if not items:
        return _fail("No titles with an overview found to probe.")

    engine = GeminiTaggingEngine(model_name=model_name)
    tagged = await engine.tag_items(items)
    usage = engine.last_usage

    tag_counts = [len(t.get("tags") or {}) for t in tagged]
    avg_tags = sum(tag_counts) / len(tag_counts) if tag_counts else 0.0
    print(f"\nTagged {len(tagged)}/{len(items)} titles, {avg_tags:.1f} tags each")

    if not usage:
        return _fail(
            "Gemini returned no usageMetadata; Curatio refuses unmetered work."
        )

    prompt_tokens = usage.get("prompt_token_count", 0)
    output_tokens = usage.get("candidates_token_count", 0)
    thoughts = usage.get("thoughts_token_count", 0)
    if thoughts:
        # Billed as output on the 3.x models, and easy to leave out of a budget.
        print(f"{WARN} {thoughts:,} reasoning tokens — these bill as OUTPUT.")
        output_tokens += thoughts

    per_in = prompt_tokens / len(items)
    per_out = output_tokens / len(items)
    print(f"\nMeasured per title: {per_in:.0f} input, {per_out:.0f} output tokens")
    print(f"  (this batch: {prompt_tokens:,} in, {output_tokens:,} out)")

    if price_in is None or price_out is None:
        print(
            f"\n{WARN} No prices given, so no cost is shown. Re-run with the rates "
            "from your\n       billing console, e.g. "
            "--price-in 0.15 --price-out 1.25"
        )
        print(f"\n       At those rates a {library_total:,}-title re-tag would use")
        print(
            f"       {per_in * library_total / 1e6:,.0f}M input and "
            f"{per_out * library_total / 1e6:,.0f}M output tokens."
        )
        return 0

    per_title = per_in / 1e6 * price_in + per_out / 1e6 * price_out
    print(f"\nAt ${price_in}/M in, ${price_out}/M out:")
    print(f"  per title      ${per_title:.6f}")
    for label, count in (
        (f"full library ({library_total:,})", library_total),
        ("100,000 titles", 100_000),
    ):
        print(f"  {label:<28} ${per_title * count:,.2f}")
    print(
        f"\n  Batch mode, where available, is typically half of this: "
        f"${per_title * library_total / 2:,.2f}"
    )
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
