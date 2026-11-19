"""
Universal category definitions and seeding.

These 40 categories are seeded automatically on app startup so the
catalog list is always populated, even before the initial tagging
build is run.

**A row's name is a promise about its contents.** Eighteen of these formulas
used to be ``min_required: 2`` over three tags, which means the row's own
subject was optional: "Mind-Bending Sci-Fi Thrillers" was satisfied by
Cerebral + Suspenseful with no Sci-Fi tag anywhere, and "Deeply Disturbing
Horror" by Dark + Intense with no Horror. That is the single largest source of
rows whose contents do not match their header.

The rule now applied throughout:

* the **head noun** -- the thing the row says these titles *are* (Crime,
  Horror, Heist, Neo-Noir) -- is ``mandatory``, so every member carries it;
* the **modifiers** stay ``required`` with ``min_required: 1``, so a member is
  at least partly the *kind* of that thing the header describes.

Ordering does the rest without a third mechanism: relevance is
``SUM(weight_norm)`` over every matched tag, so a title carrying all three
outranks one carrying two, and the head of the row is the fully-qualified
titles. A row that wants to be stricter needs one more name moved into
``mandatory``; a row that comes out too thin at that setting simply will not
render (``personalized_sync.MIN_ROW_ITEMS``), which is the intended outcome
rather than a failure.

Two-tag formulas with ``min_required: 2`` already require both tags and are
left alone.
"""

from typing import Any

from loguru import logger
from sqlalchemy.orm import Session

from app.models import UniversalCategory

#: Curated rows outrank every generated row by priority. They are
#: editorial and hand-checked; synthesis is a statistical guess.
CURATED_PRIORITY = 1000

UNIVERSAL_CATEGORIES: list[dict[str, Any]] = [
    # Tier 1: Genre + Mood (15)
    {
        "id": "dark-crime-dramas",
        "name": "Dark & Gritty Crime Dramas",
        "tier": 1,
        "media_type": "movie",
        "formula": {
            "mandatory": ["Crime"],
            "required": ["Dark", "Gritty"],
            "min_required": 1,
        },
    },
    {
        "id": "feel-good-comedies",
        "name": "Feel-Good Comedies",
        "tier": 1,
        "media_type": "movie",
        "formula": {"required": ["Feel-Good", "Comedy"], "min_required": 2},
    },
    {
        "id": "mind-bending-scifi",
        "name": "Mind-Bending Sci-Fi Thrillers",
        "tier": 1,
        "media_type": "movie",
        "formula": {
            "mandatory": ["Sci-Fi"],
            "required": ["Cerebral", "Suspenseful"],
            "min_required": 1,
        },
    },
    {
        "id": "slow-burn-thrillers",
        "name": "Slow-Burn Psychological Thrillers",
        "tier": 1,
        "media_type": "movie",
        "formula": {
            "mandatory": ["Thriller"],
            "required": ["Slow-Burn", "Cerebral"],
            "min_required": 1,
        },
    },
    {
        "id": "quirky-indie",
        "name": "Quirky Independent Films",
        "tier": 1,
        "media_type": "movie",
        "formula": {"required": ["Quirky", "Independent"], "min_required": 2},
    },
    {
        "id": "intense-action",
        "name": "High-Octane Action",
        "tier": 1,
        "media_type": "movie",
        "formula": {"required": ["Action", "Intense"], "min_required": 2},
    },
    {
        "id": "heartwarming-dramas",
        "name": "Heartwarming Dramas",
        "tier": 1,
        "media_type": "movie",
        "formula": {"required": ["Heartwarming", "Drama"], "min_required": 2},
    },
    {
        "id": "witty-comedies",
        "name": "Sharp & Witty Comedies",
        "tier": 1,
        "media_type": "tv",
        "formula": {"required": ["Witty", "Comedy"], "min_required": 2},
    },
    {
        "id": "dark-horror",
        "name": "Deeply Disturbing Horror",
        "tier": 1,
        "media_type": "movie",
        "formula": {
            "mandatory": ["Horror"],
            "required": ["Dark", "Intense"],
            "min_required": 1,
        },
    },
    {
        "id": "lighthearted-romance",
        "name": "Lighthearted Romance",
        "tier": 1,
        "media_type": "movie",
        "formula": {"required": ["Lighthearted", "Romance"], "min_required": 2},
    },
    {
        "id": "gritty-thrillers",
        "name": "Gritty Crime Thrillers",
        "tier": 1,
        "media_type": "tv",
        "formula": {
            "mandatory": ["Thriller"],
            "required": ["Gritty", "Crime"],
            "min_required": 1,
        },
    },
    {
        "id": "suspenseful-mysteries",
        "name": "Edge-of-Your-Seat Mysteries",
        "tier": 1,
        "media_type": "tv",
        "formula": {"required": ["Suspenseful", "Mystery"], "min_required": 2},
    },
    {
        "id": "cerebral-dramas",
        "name": "Cerebral Dramas That Make You Think",
        "tier": 1,
        "media_type": "movie",
        "formula": {"required": ["Cerebral", "Drama"], "min_required": 2},
    },
    {
        "id": "feel-good-fantasy",
        "name": "Feel-Good Fantasy Adventures",
        "tier": 1,
        "media_type": "movie",
        "formula": {"required": ["Feel-Good", "Fantasy"], "min_required": 2},
    },
    {
        "id": "dark-sci-fi",
        "name": "Dark & Thought-Provoking Sci-Fi",
        "tier": 1,
        "media_type": "tv",
        "formula": {
            "mandatory": ["Sci-Fi"],
            "required": ["Dark", "Cerebral"],
            "min_required": 1,
        },
    },
    # Tier 2: Era + Genre (5)
    {
        "id": "80s-action",
        "name": "Totally '80s Action",
        "tier": 2,
        "media_type": "movie",
        "formula": {
            "mandatory": ["1980s"],
            "required": ["Action"],
            "min_required": 1,
        },
    },
    {
        "id": "90s-comedies",
        "name": "'90s Comedies",
        "tier": 2,
        "media_type": "movie",
        "formula": {
            "mandatory": ["1990s"],
            "required": ["Comedy"],
            "min_required": 1,
        },
    },
    {
        "id": "70s-thrillers",
        "name": "'70s Paranoia Thrillers",
        "tier": 2,
        "media_type": "movie",
        "formula": {
            "mandatory": ["1970s"],
            "required": ["Thriller", "Suspenseful"],
            "min_required": 1,
        },
    },
    {
        "id": "golden-age-noir",
        "name": "Golden Age Film Noir",
        "tier": 2,
        "media_type": "movie",
        "formula": {
            "mandatory": ["1940s"],
            "required": ["Crime", "Dark"],
            "min_required": 1,
        },
    },
    {
        "id": "modern-horror",
        "name": "Modern Horror (2010s & Beyond)",
        "tier": 2,
        "media_type": "movie",
        "formula": {
            "mandatory": ["2010s"],
            "required": ["Horror"],
            "min_required": 1,
        },
    },
    # Tier 3: Plot Elements (6)
    {
        "id": "heist-capers",
        "name": "Heist & Caper Films",
        "tier": 3,
        "media_type": "movie",
        "formula": {
            "mandatory": ["Heist"],
            "required": ["Crime", "Suspenseful"],
            "min_required": 1,
        },
    },
    {
        "id": "time-travel-sci-fi",
        "name": "Time Travel Mind-Benders",
        "tier": 3,
        "media_type": "movie",
        "formula": {"required": ["Time Travel", "Sci-Fi"], "min_required": 2},
    },
    {
        "id": "revenge-tales",
        "name": "Cold-Blooded Revenge Stories",
        "tier": 3,
        "media_type": "movie",
        "formula": {
            "mandatory": ["Revenge"],
            "required": ["Intense", "Dark"],
            "min_required": 1,
        },
    },
    {
        "id": "courtroom-dramas",
        "name": "Courtroom Dramas",
        "tier": 3,
        "media_type": "movie",
        "formula": {"required": ["Courtroom", "Drama"], "min_required": 2},
    },
    {
        "id": "coming-of-age",
        "name": "Coming-of-Age Stories",
        "tier": 3,
        "media_type": "movie",
        "formula": {
            "mandatory": ["Coming-of-Age"],
            "required": ["Drama", "Heartwarming"],
            "min_required": 1,
        },
    },
    {
        "id": "survival-thrillers",
        "name": "Against All Odds: Survival Stories",
        "tier": 3,
        "media_type": "movie",
        "formula": {
            "mandatory": ["Survival"],
            "required": ["Intense", "Thriller"],
            "min_required": 1,
        },
    },
    # Tier 4: Style + Character (9)
    {
        "id": "neo-noir-cinema",
        "name": "Neo-Noir Cinema",
        "tier": 4,
        "media_type": "movie",
        "formula": {
            "mandatory": ["Neo-Noir"],
            "required": ["Dark", "Crime"],
            "min_required": 1,
        },
    },
    {
        "id": "arthouse-gems",
        "name": "Arthouse Cinema",
        "tier": 4,
        "media_type": "movie",
        "formula": {"required": ["Arthouse", "Cerebral"], "min_required": 2},
    },
    {
        "id": "cyberpunk-futures",
        "name": "Cyberpunk Futures",
        "tier": 4,
        "media_type": "movie",
        "formula": {
            "mandatory": ["Cyberpunk"],
            "required": ["Sci-Fi", "Neon Visuals"],
            "min_required": 1,
        },
    },
    {
        "id": "visually-stunning",
        "name": "Visually Stunning Masterpieces",
        "tier": 4,
        "media_type": "movie",
        "formula": {
            "required": ["Visually Stunning", "Drama"],
            "min_required": 2,
        },
    },
    {
        "id": "satirical-comedies",
        "name": "Biting Satire & Dark Comedy",
        "tier": 4,
        "media_type": "movie",
        "formula": {
            "mandatory": ["Satirical"],
            "required": ["Comedy", "Witty"],
            "min_required": 1,
        },
    },
    {
        "id": "anti-hero-saga",
        "name": "Anti-Hero Sagas",
        "tier": 4,
        "media_type": "tv",
        "formula": {
            "mandatory": ["Anti-Hero"],
            "required": ["Dark", "Drama"],
            "min_required": 1,
        },
    },
    {
        "id": "strong-female-leads",
        "name": "Powerful Women on Screen",
        "tier": 4,
        "media_type": "movie",
        "formula": {
            "required": ["Strong Female Lead", "Drama"],
            "min_required": 2,
        },
    },
    {
        "id": "character-studies",
        "name": "Intimate Character Studies",
        "tier": 4,
        "media_type": "movie",
        "formula": {
            "mandatory": ["Character Study"],
            "required": ["Slow-Burn", "Drama"],
            "min_required": 1,
        },
    },
    {
        "id": "underdog-stories",
        "name": "Underdog Triumphs",
        "tier": 4,
        "media_type": "movie",
        "formula": {"required": ["Underdog", "Heartwarming"], "min_required": 2},
    },
    # Tier 5: Special Collections (5)
    {
        "id": "conspiracy-thrillers",
        "name": "Conspiracy & Paranoia",
        "tier": 5,
        "media_type": "tv",
        "formula": {
            "mandatory": ["Conspiracy"],
            "required": ["Suspenseful", "Thriller"],
            "min_required": 1,
        },
    },
    {
        "id": "found-footage-horror",
        "name": "Found Footage Frights",
        "tier": 5,
        "media_type": "movie",
        "formula": {"required": ["Found Footage", "Horror"], "min_required": 2},
    },
    {
        "id": "ensemble-dramedies",
        "name": "Ensemble Cast Dramedies",
        "tier": 5,
        "media_type": "tv",
        "formula": {
            "mandatory": ["Ensemble Cast"],
            "required": ["Comedy", "Drama"],
            "min_required": 1,
        },
    },
    {
        "id": "period-costume-dramas",
        "name": "Lavish Period Dramas",
        "tier": 5,
        "media_type": "tv",
        "formula": {"required": ["Period Costume", "Drama"], "min_required": 2},
    },
    {
        "id": "detective-mysteries",
        "name": "Whodunit Detective Mysteries",
        "tier": 5,
        "media_type": "tv",
        "formula": {
            "mandatory": ["Detective"],
            "required": ["Mystery", "Suspenseful"],
            "min_required": 1,
        },
    },
]


def seed_categories(db: Session) -> int:
    """
    Seed inactive compatibility definitions for the former default rows.

    Idempotent — only inserts categories that don't already exist.
    Returns the number of new categories created.

    Owns the ``source='curated'`` compatibility tier only. Generated rows belong to
    ``app.category_synth``, and this runs on **every startup** -- without the
    scoping it would treat each synthesized row as a curated row that had
    drifted and rewrite it back, once per boot.
    """
    created = 0
    updated = 0
    for i, cat_data in enumerate(UNIVERSAL_CATEGORIES):
        existing = (
            db.query(UniversalCategory)
            .filter(UniversalCategory.id == cat_data["id"])
            .first()
        )

        if existing:
            # Sync formula, name, tier, and sort_order on every startup
            changed = False
            if existing.tag_formula != cat_data["formula"]:
                existing.tag_formula = cat_data["formula"]
                changed = True
            if existing.name != cat_data["name"]:
                existing.name = cat_data["name"]
                changed = True
            if existing.tier != cat_data["tier"]:
                existing.tier = cat_data["tier"]
                changed = True
            if existing.sort_order != i + 1:
                existing.sort_order = i + 1
                changed = True
            if existing.source != "curated":
                # An id collision between the two tiers: the curated definition
                # wins, because it is the one a human wrote.
                existing.source = "curated"
                changed = True
            # Curated rows are retained only as inactive records. Home rows and
            # rebuilds use generated rows only.
            if existing.is_active:
                existing.is_active = False
                changed = True
            if existing.manifest_priority != CURATED_PRIORITY:
                existing.manifest_priority = CURATED_PRIORITY
                changed = True
            if changed:
                updated += 1
        else:
            category = UniversalCategory(
                id=cat_data["id"],
                name=cat_data["name"],
                tier=cat_data["tier"],
                sort_order=i + 1,
                media_type=cat_data["media_type"],
                tag_formula=cat_data["formula"],
                source="curated",
                manifest_priority=CURATED_PRIORITY,
                is_active=False,
            )
            db.add(category)
            created += 1

    db.commit()

    if created or updated:
        logger.info(f"Categories: {created} created, {updated} updated")
    else:
        logger.info("All 40 universal categories already up to date")

    return created
