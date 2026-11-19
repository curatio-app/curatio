"""
Build tagging prompts from the taxonomy.

This replaces the hardcoded vocabulary that used to live inside
``gemini_client._create_tagging_prompt`` -- the third of three divergent copies
of the tag list, and the one that silently made six database tags unreachable.
The vocabulary now comes from :mod:`app.taxonomy` and cannot drift from what the
seeder writes.

Three decisions worth stating, because each is a lever on cost or quality:

**The vocabulary is per-batch, not global.** Hundreds of tags with glosses cost
thousands of tokens. Inlining all of them in every request would waste millions
of input tokens, and recall degrades when a model picks from one flat list.
:func:`app.taxonomy.shortlist` builds a <=90-tag evidence-backed menu per item,
batching clusters similar titles, and the prompt carries the union. Each title
also carries its own compact set of allowed indexes. That prevents a tag offered for one title in the
batch from leaking onto another title without duplicating vocabulary text.

**Output is schema-constrained.** Tags are small ``name``/``confidence`` objects
because Gemini's structured-output schema can validate that shape. It is a few
tokens larger than a pair, but it removes malformed-key and prose responses --
both materially more expensive when they trigger another paid request.

**"extra" is an explicit escape hatch.** The old code silently dropped anything
off-vocabulary. Asking for it by name converts that loss into a measurable
signal that the admin QA view ranks by frequency.
"""

from __future__ import annotations

from typing import Any, Sequence

from app.taxonomy import (
    FACETS,
    TagDef,
    model_batch_vocabulary,
    model_shortlist,
    prompt_shard,
)

#: Fixed instruction block. Byte-identical on every call so it caches well and
#: so a prompt change is a visible diff rather than an interpolation accident.
SYSTEM_BLOCK = """\
You are a film and television cataloguer. For each title, choose the tags that \
genuinely apply from the ALLOWED TAGS list.

RULES
- Choose ONLY from the tag indexes printed as allowed for THAT TITLE. Return the
  integer as "tag_index"; never invent an index and never return or reword the
  tag name. An index allowed for another title is not allowed for this one.
- Choose 0-3 precise interpretive tags. Returning zero is valid and preferred
  to a weak guess. The server deterministically adds genres, adaptations,
  formats, settings, plot facts, maturity facts, country, release decade,
  runtime and series-length labels, so never duplicate those facts.
- Use at most 1 mood tag, at most 2 theme tags, and at most 1 character or
  pacing tag. Do not fill a quota.
- Prefer the specific over the generic: "Slasher" beats "Horror" when both fit.
- Score 1.0 defining - 0.85 strong - 0.70 clearly applicable - 0.60 uncertain.
  Never use 0.60 as a default or filler. Mood and pacing tags scored below 0.65
  are rejected by the server; omit them when the whole-work evidence is weak.
- Never choose contradictory concepts (for example Slow-Burn with Fast-Paced,
  Anthology with Serialized, or Adult Animation with Kids/All Ages).
- Anthology means a genuinely different story each episode or season. It is not
  a synonym for episodic, multi-season or ensemble television.
- Mood and theme tags must describe the viewing experience as a whole. A single
  relationship does not make a work Romantic; one sad scene is not Bittersweet;
  a flawed supporting character does not make the protagonist an Anti-Hero.
- Do not call violent, horrific or war-driven material Cozy, Feel-Good,
  Heartwarming, Hopeful or Lighthearted unless that positive tone clearly
  dominates the whole work.
- Do not infer Fast-Paced, Slow-Burn or Twisty from Action/Adventure genre or a
  busy synopsis. Choose pacing only when the evidence describes the sustained
  pace or structure of the complete work.
- Themes must be central, not merely mentioned. AI Ethics requires conflict
  about machine conduct or rights; the presence of a robot is insufficient.
  Mortality requires the work to meaningfully confront death, dying or
  immortality; danger or competition alone is insufficient.
- Judge the work itself, not its popularity or how well-known it is.
- If a reusable, strongly-applicable category concept has no tag, put at most
  two short noun phrases in "extra". Never put summaries or sentences there.

OUTPUT
Follow the provider's response schema exactly and return every requested TMDB ID
once. Return JSON only: no markdown fence and no commentary.\
"""


def _compact_indexes(indexes: Sequence[int]) -> str:
    """Render sorted integers as compact ranges (``0-3,7,10-12``)."""
    values = sorted(set(int(index) for index in indexes))
    if not values:
        return "none"
    ranges: list[str] = []
    start = previous = values[0]
    for value in values[1:]:
        if value == previous + 1:
            previous = value
            continue
        ranges.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = value
    ranges.append(str(start) if start == previous else f"{start}-{previous}")
    return ",".join(ranges)


def _allowed_indexes_by_facet(
    vocabulary: Sequence[TagDef], allowed_indexes: Sequence[int]
) -> str:
    grouped: dict[str, list[int]] = {}
    for index in sorted(set(int(value) for value in allowed_indexes)):
        if 0 <= index < len(vocabulary):
            grouped.setdefault(vocabulary[index].facet, []).append(index)
    return (
        "; ".join(
            f"{facet}={_compact_indexes(grouped[facet])}"
            for facet in FACETS
            if grouped.get(facet)
        )
        or "none"
    )


def _format_item(
    features: dict[str, Any],
    allowed_indexes: Sequence[int],
    vocabulary: Sequence[TagDef],
) -> str:
    """One title's block.

    Compact by design: at 40 items per request this is most of the input cost,
    and every field here has to earn its tokens.
    """
    tmdb_id = features.get("tmdb_id")
    media_type = features.get("media_type") or "movie"
    year = str(features.get("release_date") or "")[:4]

    head = [str(tmdb_id), media_type]
    if year:
        head.append(year)

    facts = features.get("tag_facts") or {}
    mdb = features.get("mdb_facts") or {}

    runtime = facts.get("runtime")
    if runtime:
        head.append(f"{runtime}min")
    cert = facts.get("certification") or mdb.get("certification")
    if cert:
        head.append(str(cert))

    lines = [f"### {' | '.join(head)}"]
    lines.append(
        "Allowed tag_index values by facet: "
        + _allowed_indexes_by_facet(vocabulary, allowed_indexes)
    )
    lines.append(f"Title: {features.get('title') or 'Unknown'}")

    origin = []
    if facts.get("countries"):
        origin.append("/".join(str(c) for c in facts["countries"][:3]))
    if features.get("original_language"):
        origin.append(str(features["original_language"]))
    if origin:
        lines.append(f"Origin: {' · '.join(origin)}")

    genres = features.get("genres") or []
    if genres:
        lines.append(f"Genres: {', '.join(str(g) for g in genres)}")

    # TMDB keywords are the highest-signal field available and were discarded
    # entirely before enrichment existed.
    keywords = list(facts.get("keywords") or []) + list(mdb.get("keywords") or [])
    seen: set[str] = set()
    deduped = []
    for keyword in keywords:
        key = str(keyword).strip().lower()
        if key and key not in seen:
            seen.add(key)
            deduped.append(str(keyword))
    if deduped:
        lines.append(f"Keywords: {', '.join(deduped[:15])}")

    people = []
    if facts.get("directors"):
        people.append("Dir: " + ", ".join(str(d) for d in facts["directors"][:2]))
    if facts.get("cast"):
        people.append("Cast: " + ", ".join(str(c) for c in facts["cast"][:5]))
    if people:
        lines.append(" · ".join(people))

    for label, key in (("Studio", "studios"), ("Network", "networks")):
        value = facts.get(key)
        if value:
            lines.append(f"{label}: {', '.join(str(v) for v in value[:2])}")
    if facts.get("collection"):
        lines.append(f"Collection: {facts['collection']}")

    overview = " ".join(str(features.get("overview") or "").split())
    if overview:
        lines.append(f"Overview: {overview[:700]}")

    return "\n".join(lines)


def build_prompt(
    batch: Sequence[dict[str, Any]], vocabulary: Sequence[TagDef] | None = None
) -> tuple[str, list[TagDef]]:
    """Render the prompt for one batch.

    Returns ``(prompt, vocabulary)``. The vocabulary is returned so the caller
    can pass the same tag names to the validator, which confines fuzzy matching
    to what the model was actually offered -- without that, "Noir Comedy" would
    resolve to "Neo-Noir".
    """
    vocab = (
        list(vocabulary) if vocabulary is not None else model_batch_vocabulary(batch)
    )
    parts = [
        SYSTEM_BLOCK,
        "",
        "ALLOWED TAGS — choose ONLY from this list:",
        # Compact integer choices let Gemini enforce a closed vocabulary
        # without placing 120-180 long strings in a repeated schema enum. The
        # latter exceeded the provider's schema-state limit in production.
        prompt_shard(vocab, indexed=True),
        "",
        "TITLES:",
    ]
    index_by_name = {tag.name: index for index, tag in enumerate(vocab)}
    for item in batch:
        allowed_indexes = [
            index_by_name[tag.name]
            for tag in model_shortlist(item)
            if tag.name in index_by_name
        ]
        parts.append(_format_item(item, allowed_indexes, vocab))
    return "\n".join(parts), vocab


def shortlist_names(features: dict[str, Any]) -> list[str]:
    """Canonical names offered for one item, for the validator's fuzzy bound."""
    return [tag.name for tag in model_shortlist(features)]
