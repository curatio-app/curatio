"""
Validation layer between raw model output and ``movie_tags`` rows.

Before this module, a tag the model returned that wasn't in the 75-name
vocabulary was dropped on the floor with no log and no metric -- a single
``if tag_name in tag_map:`` in each of two workers. There was no way to know
whether the model was producing 2% or 40% off-vocabulary output.

Every unresolved string now lands in ``tag_observations`` with a sample title,
so the admin QA dashboard can rank them by frequency and an operator can promote
one to an alias without a redeploy. That is the whole governance loop.

Resolution order, cheapest first:

1. exact canonical name
2. ``tag_aliases`` (DB, operator-managed) then the static taxonomy aliases
3. normalized form
4. bounded fuzzy -- difflib >= 0.90, **against that item's shortlist only**
5. an independent canonical-name check against that item's shortlist

Step 4 is deliberately narrow. Matching against the whole taxonomy
would turn "Noir Comedy" into "Neo-Noir"; matching against the <=90 tags the
model was actually offered for this item fixes "Neo Noir" and little else.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from app.taxonomy import (
    ALIAS_INDEX,
    BY_NAME,
    TagDef,
    normalize,
)

__all__ = [
    "normalize",
    "TagResolution",
    "ValidationResult",
    "resolve_tag",
    "validate_item_tags",
    "DEFAULT_MIN_CONFIDENCE",
    "DEFAULT_MAX_PER_ITEM",
    "ABSOLUTE_MAX_PER_ITEM",
    "MAX_MODEL_TAGS",
    "FACET_MIN_CONFIDENCE",
    "MODEL_FACET_CAPS",
    "CONFLICT_PAIRS",
    "VALIDATION_SCHEMA_VERSION",
    "MIN_SURVIVORS",
    "MIN_TOTAL_TAGS",
    "FUZZY_CUTOFF",
]

#: Included in ``tagger_version``. Increment whenever acceptance, conflict or
#: canonicalisation semantics change so stale rows cannot masquerade as current.
VALIDATION_SCHEMA_VERSION = "7"

#: Mirrors settings.tag_min_confidence / tag_max_per_item. Passed in explicitly
#: by callers so this module stays importable without app.config.
DEFAULT_MIN_CONFIDENCE = 0.60
DEFAULT_MAX_PER_ITEM = 22
ABSOLUTE_MAX_PER_ITEM = 22
MAX_MODEL_TAGS = 3

# Job 224 showed that the nano model used exactly 0.60 as a catch-all for weak
# tonal guesses: 729 mood/pacing assignments landed on that boundary. Concrete
# concepts such as Heist or Based on a Book can still be useful at the global
# floor, but subjective viewing-experience labels need a separate quality bar.
# Operator settings above these values still win through max() below.
FACET_MIN_CONFIDENCE: dict[str, float] = {
    "mood": 0.65,
    "pacing_format": 0.65,
}

#: Limits make one overactive facet unable to crowd defining genres and themes
#: out of the stored set. Derived facts do not consume these model budgets.
MODEL_FACET_CAPS: dict[str, int] = {
    "subgenre": 2,
    "mood": 1,
    "theme": 2,
    "plot_device": 1,
    "setting_period": 1,
    "place": 1,
    "origin": 1,
    "style": 1,
    "character": 1,
    "audience": 1,
    "maturity": 2,
    "pacing_format": 1,
    "source_franchise": 1,
    "seasonal": 1,
    "pedigree": 1,
}

#: Old canonical rows remain in the database for compatibility, but new output
#: collapses semantic duplicates onto one concept so category scores are not
#: double-weighted.
CANONICAL_EQUIVALENTS: dict[str, str] = {
    "Coming-of-Age": "Coming of Age",
    "Ensemble": "Ensemble Cast",
    "Anthology Structure": "Anthology",
    "Animated for Adults": "Adult Animation",
    "Mockumentary Style": "Mockumentary",
}

#: Conservative impossibilities observed in the production pilot. Pairs are
#: sorted so membership checks stay cheap and deterministic.
CONFLICT_PAIRS: frozenset[frozenset[str]] = frozenset(
    frozenset(pair)
    for pair in (
        ("Slow-Burn", "Fast-Paced"),
        ("Slow-Burn", "Breakneck"),
        ("Meditative Pace", "Fast-Paced"),
        ("Meditative Pace", "Breakneck"),
        ("Anthology", "Serialized"),
        ("Anthology", "Episodic"),
        ("Adult Animation", "Kids"),
        ("Adult Animation", "Preschool"),
        ("Adult Animation", "All Ages"),
        ("Adult Animation", "Family Movie Night"),
        # Job 221 showed that a small model will happily spend its quota on
        # mutually opposed tone words. These are deliberately conservative:
        # nuanced combinations such as Bittersweet + Hopeful remain legal.
        ("Bleak", "Feel-Good"),
        ("Bleak", "Heartwarming"),
        ("Bleak", "Lighthearted"),
        ("Bleak", "Cozy"),
        ("Bleak", "Whimsical"),
        ("Brutal", "Heartwarming"),
        ("Brutal", "Lighthearted"),
        ("Brutal", "Cozy"),
        ("Chilling", "Feel-Good"),
        ("Chilling", "Heartwarming"),
        ("Chilling", "Lighthearted"),
        ("Chilling", "Cozy"),
        ("Dark", "Feel-Good"),
        ("Dark", "Heartwarming"),
        ("Unsettling", "Feel-Good"),
        ("Unsettling", "Heartwarming"),
    )
)

#: Model judgements are optional in taxonomy v5. Exact factual semantics are
#: computed locally, so forcing even one model tag recreates the filler problem
#: seen across jobs 221-227.
MIN_SURVIVORS = 0

#: A completed title must still have at least one factual or interpretive tag.
#: Normal eligible records have several deterministic facts; this only protects
#: direct callers and malformed metadata rows from publishing an empty set.
MIN_TOTAL_TAGS = 1

#: difflib ratio below which a fuzzy match is refused.
FUZZY_CUTOFF = 0.90


@dataclass(frozen=True, slots=True)
class TagResolution:
    """One raw tag string, resolved or not."""

    raw: str
    confidence: float
    tag: TagDef | None = None
    method: str = "unmapped"  # exact | alias | db_alias | normalized | fuzzy | unmapped
    reason: str = ""

    @property
    def resolved(self) -> bool:
        return self.tag is not None


@dataclass(slots=True)
class ValidationResult:
    """Outcome of validating one item's tags."""

    tmdb_id: int
    media_type: str
    accepted: list[TagResolution] = field(default_factory=list)
    rejected: list[TagResolution] = field(default_factory=list)
    unresolved: list[TagResolution] = field(default_factory=list)
    requeue: bool = False

    @property
    def rows(self) -> list[tuple[str, float]]:
        """(canonical_name, confidence) pairs ready for upsert."""
        return [(r.tag.name, r.confidence) for r in self.accepted if r.tag]

    @property
    def stats(self) -> dict[str, int]:
        return {
            "accepted": len(self.accepted),
            "rejected": len(self.rejected),
            "unresolved": len(self.unresolved),
        }


def _clamp(value: Any) -> float | None:
    """Coerce a model-supplied confidence to [0, 1], or None if unusable."""
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    if score != score:  # NaN
        return None
    return max(0.0, min(1.0, score))


def resolve_tag(
    raw: str,
    *,
    shortlist_names: Sequence[str] | None = None,
    db_aliases: Mapping[str, str] | None = None,
) -> tuple[TagDef | None, str]:
    """Resolve one raw string to a canonical tag.

    Args:
        raw: The string the model emitted.
        shortlist_names: Canonical names offered for this item. Fuzzy matching
            is restricted to these. :func:`validate_item_tags` additionally
            rejects every resolved tag outside this set, including exact names
            and aliases; pass None only for trusted/non-model callers.
        db_aliases: normalized -> canonical, from the ``tag_aliases`` table.
            Operator-managed entries win over the static taxonomy ones.

    Returns:
        ``(TagDef | None, method)``.
    """
    if not raw or not str(raw).strip():
        return None, "unmapped"

    text = str(raw).strip()

    # 1. exact canonical
    if text in BY_NAME:
        return BY_NAME[text], "exact"

    key = normalize(text)
    if not key:
        return None, "unmapped"

    # 2a. operator-managed aliases from the database
    if db_aliases:
        name = db_aliases.get(key)
        if name and name in BY_NAME:
            return BY_NAME[name], "db_alias"

    # 2b. static taxonomy aliases (also covers normalized canonical names)
    name = ALIAS_INDEX.get(key)
    if name:
        # ALIAS_INDEX maps normalized canonical names to themselves, so
        # distinguish a plain case/punctuation fix from a real alias hit.
        return BY_NAME[name], "normalized" if normalize(name) == key else "alias"

    # 3. bounded fuzzy, against this item's shortlist only
    if shortlist_names:
        candidates = {normalize(n): n for n in shortlist_names if n in BY_NAME}
        match = difflib.get_close_matches(
            key, list(candidates), n=1, cutoff=FUZZY_CUTOFF
        )
        if match:
            return BY_NAME[candidates[match[0]]], "fuzzy"

    return None, "unmapped"


def validate_item_tags(
    tmdb_id: int,
    media_type: str,
    raw_tags: Iterable[Any],
    *,
    shortlist_names: Sequence[str] | None = None,
    db_aliases: Mapping[str, str] | None = None,
    derived: Mapping[str, float] | None = None,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    max_per_item: int = DEFAULT_MAX_PER_ITEM,
) -> ValidationResult:
    """Validate one item's tags.

    Args:
        raw_tags: Either ``[{"name": "Heist", "confidence": 0.95}, ...]``
            (the structured-output shape), ``[["Heist", 0.95], ...]``, or
            ``{"Heist": 0.95}``. All are accepted so a prompt migration cannot
            silently drop a batch.
        derived: Tags computed by ``taxonomy.derive_tags``. Merged in at
            confidence 1.0 and exempt from the confidence floor -- they are facts.

    Returns:
        A :class:`ValidationResult`. Model tags are optional; ``requeue`` is
        set only when the combined deterministic and model result is empty.
    """
    pairs: list[tuple[str, Any]] = []
    if isinstance(raw_tags, Mapping):
        pairs = list(raw_tags.items())
    else:
        for entry in raw_tags or ():
            if isinstance(entry, Mapping):
                name = entry.get("name") or entry.get("tag")
                if name is not None:
                    pairs.append((name, entry.get("confidence", entry.get("score"))))
            elif isinstance(entry, (list, tuple)) and len(entry) >= 2:
                pairs.append((entry[0], entry[1]))
            elif isinstance(entry, str):
                pairs.append((entry, 1.0))

    result = ValidationResult(tmdb_id=tmdb_id, media_type=media_type)
    seen: dict[str, TagResolution] = {}
    allowed = set(shortlist_names) if shortlist_names is not None else None

    for raw, raw_score in pairs:
        raw_text = "" if raw is None else str(raw).strip()
        score = _clamp(raw_score)

        if score is None:
            result.rejected.append(
                TagResolution(raw_text, 0.0, reason="unparseable confidence")
            )
            continue

        tag, method = resolve_tag(
            raw_text, shortlist_names=shortlist_names, db_aliases=db_aliases
        )

        if tag is None:
            result.unresolved.append(
                TagResolution(raw_text, score, reason="off-vocabulary")
            )
            continue

        if tag.derived:
            # The model is not offered derived tags; if it invents one, prefer
            # the computed value and don't let the guess through.
            result.rejected.append(
                TagResolution(raw_text, score, tag, method, "derived tag from model")
            )
            continue

        # Resolution proves vocabulary membership, not title membership. The
        # provider schema carries the union for a whole batch, so this check is
        # the server-side boundary that prevents one title from accepting an
        # exact canonical index offered only for a neighbour in that batch.
        if allowed is not None and tag.name not in allowed:
            result.rejected.append(
                TagResolution(raw_text, score, tag, method, "not offered for title")
            )
            continue

        preferred = CANONICAL_EQUIVALENTS.get(tag.name)
        if preferred:
            canonical = BY_NAME.get(preferred)
            if canonical is not None and media_type in canonical.media:
                tag = canonical
                method = "canonicalized"

        required_confidence = max(
            min_confidence, FACET_MIN_CONFIDENCE.get(tag.facet, min_confidence)
        )
        if score < required_confidence:
            reason = (
                "below facet confidence floor"
                if required_confidence > min_confidence
                else "below confidence floor"
            )
            result.rejected.append(TagResolution(raw_text, score, tag, method, reason))
            continue

        if media_type not in tag.media:
            result.rejected.append(
                TagResolution(raw_text, score, tag, method, "media type not allowed")
            )
            continue

        existing = seen.get(tag.name)
        if existing is None or score > existing.confidence:
            seen[tag.name] = TagResolution(raw_text, score, tag, method)

    for name, score in (derived or {}).items():
        tag = BY_NAME.get(name)
        if tag is None or media_type not in tag.media:
            continue
        clamped = _clamp(score)
        if clamped is None:
            continue
        seen[name] = TagResolution(name, clamped, tag, "derived")

    ordered = sorted(seen.values(), key=lambda r: (-r.confidence, r.tag.name))

    # Derived tags are facts and survive model facet/conflict budgets. The hard
    # total ceiling still applies, although normal metadata produces far fewer
    # derived facts than it could ever exhaust.
    effective_cap = min(max(0, int(max_per_item)), ABSOLUTE_MAX_PER_ITEM)
    kept: list[TagResolution] = [r for r in ordered if r.method == "derived"]
    if len(kept) > effective_cap:
        kept = kept[:effective_cap]
    budget = min(MAX_MODEL_TAGS, max(0, effective_cap - len(kept)))
    facet_counts: dict[str, int] = {}
    kept_names = {r.tag.name for r in kept if r.tag is not None}
    for res in ordered:
        if res.method == "derived":
            continue
        if budget <= 0:
            result.rejected.append(
                TagResolution(res.raw, res.confidence, res.tag, res.method, "over cap")
            )
            continue
        tag = res.tag
        if tag is None:
            continue
        conflict = next(
            (
                kept_name
                for kept_name in kept_names
                if frozenset((tag.name, kept_name)) in CONFLICT_PAIRS
            ),
            None,
        )
        if conflict:
            result.rejected.append(
                TagResolution(
                    res.raw,
                    res.confidence,
                    tag,
                    res.method,
                    f"conflicts with {conflict}",
                )
            )
            continue
        facet_cap = MODEL_FACET_CAPS.get(tag.facet, MAX_MODEL_TAGS)
        if facet_counts.get(tag.facet, 0) >= facet_cap:
            result.rejected.append(
                TagResolution(
                    res.raw,
                    res.confidence,
                    tag,
                    res.method,
                    "facet cap",
                )
            )
            continue
        kept.append(res)
        kept_names.add(tag.name)
        facet_counts[tag.facet] = facet_counts.get(tag.facet, 0) + 1
        budget -= 1

    result.accepted = sorted(kept, key=lambda r: (-r.confidence, r.tag.name))
    model_assigned = [r for r in result.accepted if r.method != "derived"]
    result.requeue = (
        len(model_assigned) < MIN_SURVIVORS or len(result.accepted) < MIN_TOTAL_TAGS
    )
    return result
