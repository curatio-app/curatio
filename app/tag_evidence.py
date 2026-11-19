"""Shared admission rules for paid tagging and recommendation rows.

The model can only classify what the source record describes.  A title, genre
and country are enough to derive factual labels, but they are not enough to
judge mood, theme or plot without guessing.  Keep that weak tail out of the
paid corpus and out of generated rows.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func


# Forty characters excludes blank/thin placeholder synopses without rejecting
# normal concise TMDB overviews.  In the 1,000-title production pilot every
# non-blank overview already cleared this value; all eight blank rows were TV
# titles whose model output collapsed to generic mood pairs.
MIN_CATALOG_OVERVIEW_CHARS = 40


def has_prompt_evidence(features: dict[str, Any]) -> bool:
    """Whether an item has prose from which semantic tags can be judged."""

    return bool(str(features.get("overview") or "").strip())


def catalog_evidence_clause():
    """SQL predicate matching records with a substantive synopsis.

    Imported lazily so taxonomy/prompt-only tooling does not initialize the
    ORM merely by importing this small policy module.
    """

    from app.models import MediaMetadata

    return (
        func.length(func.trim(func.coalesce(MediaMetadata.overview, "")))
        >= MIN_CATALOG_OVERVIEW_CHARS
    )
