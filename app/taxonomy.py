"""
Single source of truth for the Curatio tag taxonomy.

Everything that needs to know what a tag *is* reads from this module: the
database seeder (``sync_taxonomy``), the Gemini prompt builder, the validation
layer, and the category/row generators.

Before this module existed the vocabulary lived in three places that had drifted
apart -- ``workers/initial_build.py`` (the DB seed), the prompt string in
``app/gemini_client.py``, and a dead ``tag_categories`` dict in the same file.
Six tags (Western, War, Fast-Paced, European, Ensemble, Reluctant Hero) existed
as database rows but were absent from the prompt, so no title could ever receive
them. ``tests/test_taxonomy.py`` now makes that class of drift a test failure.

Two invariants are load-bearing:

* **Every one of the 75 legacy tag names survives as a canonical name.** They are
  referenced by ~2.2M ``movie_tags`` rows; renaming or dropping one orphans them.
  Facets and glosses may change, names may not.
* **``derived`` tags are never offered to the model.** Release decade, origin and
  the runtime/format tags are computed from TMDB metadata, which removes the
  largest hallucination class (the model guessing an era from vibes) and keeps
  the prompt shorter.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Sequence

TAXONOMY_VERSION = "5.1.0"

#: Facet identifiers. Stored verbatim in ``tags.category`` (String(50); the
#: longest here is "source_franchise" at 16 chars).
FACETS: tuple[str, ...] = (
    "subgenre",
    "mood",
    "theme",
    "plot_device",
    "setting_period",
    "place",
    "origin",
    "style",
    "character",
    "audience",
    "maturity",
    "pacing_format",
    "source_franchise",
    "seasonal",
    "pedigree",
    "release_decade",
)


@dataclass(frozen=True, slots=True)
class TagDef:
    """One tag in the taxonomy.

    Attributes:
        name: Canonical display name. Stored verbatim in ``tags.name``.
        facet: One of :data:`FACETS`. Stored in ``tags.category``.
        gloss: <=8 words. Doubles as the prompt line and the chip tooltip, and
            is stored in ``tags.description`` (which has never been populated).
        aliases: Extra match keys for near-miss model output. Matched after
            normalization, so case/punctuation/spacing variants need no entry.
        parent: Canonical name of a broader tag, or None.
        display: Whether the tag is shown to users as a chip.
        maturity: 0 all-ages .. 4 adults-only.
        media: Media types the tag may apply to.
        derived: Computed by :func:`derive_tags`, never offered to the model.
        sort_hint: Chip ordering; lower sorts first.
    """

    name: str
    facet: str
    gloss: str
    aliases: tuple[str, ...] = ()
    parent: str | None = None
    display: bool = True
    maturity: int = 0
    media: tuple[str, ...] = ("movie", "tv")
    derived: bool = False
    sort_hint: int = 100

    @property
    def kid_safe(self) -> bool:
        """True when the tag is appropriate for a kids profile."""
        return self.maturity <= 1


def _t(
    name: str, facet: str, gloss: str, aliases: Sequence[str] = (), **kw: Any
) -> TagDef:
    """Terse TagDef constructor used by the vocabulary tables below."""
    return TagDef(name=name, facet=facet, gloss=gloss, aliases=tuple(aliases), **kw)


# ---------------------------------------------------------------------------
# normalize() -- the shared match key
# ---------------------------------------------------------------------------
_PUNCT_RE = re.compile(r"[^\w\s]+", re.UNICODE)
_WS_RE = re.compile(r"\s+")
_SCIFI_RE = re.compile(r"\bsci\s*fi\b")


def normalize(value: str) -> str:
    """Reduce a tag string to its match key.

    Casefold, strip diacritics, punctuation to space, collapse whitespace, and
    fold the one spelling split that shows up constantly in model output
    ("sci fi" / "scifi" / "science fiction" all reach "sci-fi" via aliases).

    Because this handles case, punctuation and spacing, a TagDef only needs an
    alias for genuinely different *wordings* -- "Science Fiction", not "sci-fi".
    """
    if not value:
        return ""
    text = unicodedata.normalize("NFKD", str(value))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.casefold()
    text = _PUNCT_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text).strip()
    # Punctuation is already gone by this point, so "Sci-Fi", "sci fi" and
    # "SciFi" all arrive here as variants that must collapse to one key.
    text = _SCIFI_RE.sub("scifi", text)
    return text


# ---------------------------------------------------------------------------
# Vocabulary
#
# Legacy names (the 75 seeded by workers/initial_build.py before this module)
# are marked "L" in a trailing comment so the R1 invariant is auditable by eye
# as well as by test.
# ---------------------------------------------------------------------------

_SUBGENRE: tuple[TagDef, ...] = (
    # --- broad roots: all 14 legacy "genre" tags live here ---
    _t("Action", "subgenre", "physical stunts, chases, combat", sort_hint=1),  # L
    _t(
        "Adventure",
        "subgenre",
        "journeys into the unknown",
        ("action adventure",),
        sort_hint=1,
    ),
    _t(
        "Animation",
        "subgenre",
        "animated rather than live action",
        ("animated",),
        sort_hint=1,
    ),  # L
    _t("Comedy", "subgenre", "built to make you laugh", sort_hint=1),  # L
    _t("Crime", "subgenre", "criminals, police, or the underworld", sort_hint=1),  # L
    _t(
        "Documentary",
        "subgenre",
        "non-fiction filmmaking",
        ("docu", "nonfiction"),
        sort_hint=1,
    ),  # L
    _t("Drama", "subgenre", "serious character-driven storytelling", sort_hint=1),  # L
    _t("Family", "subgenre", "stories made for family audiences", sort_hint=1),
    _t("Fantasy", "subgenre", "magic and invented worlds", sort_hint=1),  # L
    _t("History", "subgenre", "people and events from history", sort_hint=1),
    _t("Horror", "subgenre", "made to frighten", sort_hint=1, maturity=2),  # L
    _t("Music", "subgenre", "music and musicians drive the work", sort_hint=1),
    _t("Musical", "subgenre", "characters sing the story", sort_hint=1),
    _t("Mystery", "subgenre", "a puzzle the audience solves along", sort_hint=1),  # L
    _t(
        "Romance",
        "subgenre",
        "love story at the centre",
        ("love story",),
        sort_hint=1,
    ),  # L
    _t(
        "Sci-Fi",
        "subgenre",
        "speculative science and technology",
        ("science fiction", "scifi", "sf"),
        sort_hint=1,
    ),  # L
    _t("Thriller", "subgenre", "sustained tension and danger", sort_hint=1),  # L
    _t(
        "War",
        "subgenre",
        "armed conflict and its soldiers",
        ("war film", "military"),
        sort_hint=1,
    ),  # L
    _t("Western", "subgenre", "the American frontier", ("cowboy",), sort_hint=1),  # L
    _t(
        "Political Drama",
        "subgenre",
        "government and political power",
        ("political series",),
        parent="Drama",
    ),
    # --- crime / thriller ---
    _t(
        "Neo-Noir",
        "subgenre",
        "modern noir styling and fatalism",
        ("neonoir",),
        parent="Crime",
    ),  # L
    _t(
        "Film Noir",
        "subgenre",
        "classic-era shadowy crime cinema",
        ("noir",),
        parent="Crime",
    ),
    _t(
        "Heist Thriller",
        "subgenre",
        "planning and pulling off a robbery",
        parent="Crime",
    ),
    _t(
        "Police Procedural",
        "subgenre",
        "investigation shown step by step",
        ("cop show",),
        parent="Crime",
    ),
    _t("Legal Drama", "subgenre", "lawyers, trials and verdicts", parent="Drama"),
    _t(
        "Prison Drama", "subgenre", "life behind bars", ("prison film",), parent="Drama"
    ),
    _t(
        "Spy Thriller",
        "subgenre",
        "espionage and tradecraft",
        ("espionage", "spy", "secret agent", "foreign intrigue", "tradecraft"),
        parent="Thriller",
    ),
    _t(
        "Political Thriller",
        "subgenre",
        "power, corruption and statecraft",
        parent="Thriller",
    ),
    _t(
        "Techno-Thriller", "subgenre", "technology drives the danger", parent="Thriller"
    ),
    _t("Psychological Thriller", "subgenre", "the threat is mental", parent="Thriller"),
    _t(
        "Erotic Thriller",
        "subgenre",
        "desire entangled with danger",
        parent="Thriller",
        maturity=3,
    ),
    _t(
        "Courtroom Thriller",
        "subgenre",
        "trial with the stakes of a thriller",
        parent="Thriller",
    ),
    _t(
        "Gangster Epic",
        "subgenre",
        "organised crime across decades",
        ("mob movie", "mafia"),
        parent="Crime",
    ),
    _t(
        "Caper",
        "subgenre",
        "light-hearted crime with flair",
        ("caper comedy",),
        parent="Crime",
    ),
    _t("True Crime", "subgenre", "real cases reconstructed", parent="Documentary"),
    _t(
        "Cozy Mystery",
        "subgenre",
        "gentle amateur sleuthing",
        ("cosy mystery",),
        parent="Mystery",
    ),
    _t(
        "Whodunit Mystery",
        "subgenre",
        "identify the culprit before the reveal",
        ("murder mystery",),
        parent="Mystery",
    ),
    _t("Noir Comedy", "subgenre", "crime plotting played for laughs", parent="Comedy"),
    _t(
        "Vigilante Action", "subgenre", "one person dispensing justice", parent="Action"
    ),
    _t(
        "Assassin Thriller",
        "subgenre",
        "contract killers and their targets",
        parent="Thriller",
    ),
    # --- horror ---
    _t("Slasher", "subgenre", "a killer stalks a group", parent="Horror", maturity=3),
    _t(
        "Body Horror",
        "subgenre",
        "the flesh itself betrays",
        parent="Horror",
        maturity=3,
    ),
    _t(
        "Cosmic Horror",
        "subgenre",
        "vast indifferent forces",
        ("lovecraftian",),
        parent="Horror",
        maturity=2,
    ),
    _t(
        "Folk Horror",
        "subgenre",
        "rural ritual and old belief",
        parent="Horror",
        maturity=2,
    ),
    _t(
        "Supernatural Horror",
        "subgenre",
        "ghosts, demons, possession",
        parent="Horror",
        maturity=2,
    ),
    _t(
        "Haunted House",
        "subgenre",
        "the building is the antagonist",
        parent="Horror",
        maturity=2,
    ),
    _t(
        "Zombie Horror",
        "subgenre",
        "the walking dead",
        ("zombie",),
        parent="Horror",
        maturity=3,
    ),
    _t(
        "Vampire Horror",
        "subgenre",
        "bloodsuckers and their prey",
        ("vampire",),
        parent="Horror",
        maturity=2,
    ),
    _t(
        "Creature Feature",
        "subgenre",
        "a monster on the loose",
        ("monster movie",),
        parent="Horror",
    ),
    _t(
        "Psychological Horror",
        "subgenre",
        "dread built from the mind",
        parent="Horror",
        maturity=2,
    ),
    _t(
        "Giallo",
        "subgenre",
        "stylised Italian murder mystery",
        parent="Horror",
        maturity=3,
    ),
    _t(
        "Splatter Horror",
        "subgenre",
        "extreme gore as spectacle",
        ("gorefest", "splatter"),
        parent="Horror",
        maturity=4,
    ),
    _t(
        "Horror Comedy",
        "subgenre",
        "scares played for laughs",
        parent="Horror",
        maturity=2,
    ),
    _t(
        "Home Invasion",
        "subgenre",
        "safety breached at home",
        parent="Horror",
        maturity=3,
    ),
    _t(
        "Occult Horror",
        "subgenre",
        "cults, rites and forbidden knowledge",
        parent="Horror",
        maturity=3,
    ),
    # --- sci-fi / fantasy ---
    _t("Space Opera", "subgenre", "grand adventure across the stars", parent="Sci-Fi"),
    _t("Cyberpunk", "subgenre", "high tech, low life", parent="Sci-Fi"),  # L
    _t("Dystopian", "subgenre", "a society gone wrong", ("dystopia",), parent="Sci-Fi"),
    _t(
        "Post-Apocalyptic",
        "subgenre",
        "after the collapse",
        ("apocalyptic",),
        parent="Sci-Fi",
    ),
    _t("Time Travel Sci-Fi", "subgenre", "moving through time", parent="Sci-Fi"),
    _t("Alien Invasion", "subgenre", "contact turns hostile", parent="Sci-Fi"),
    _t("First Contact", "subgenre", "meeting another intelligence", parent="Sci-Fi"),
    _t("Hard Sci-Fi", "subgenre", "rigorous about its science", parent="Sci-Fi"),
    _t(
        "Kaiju",
        "subgenre",
        "giant monsters levelling cities",
        ("giant monster",),
        parent="Sci-Fi",
    ),
    _t("Mecha", "subgenre", "piloted giant robots", parent="Sci-Fi"),
    _t("Biopunk", "subgenre", "genetic engineering gone strange", parent="Sci-Fi"),
    _t("Steampunk", "subgenre", "Victorian machines and airships", parent="Fantasy"),
    _t(
        "High Fantasy",
        "subgenre",
        "invented worlds with deep lore",
        ("epic fantasy",),
        parent="Fantasy",
    ),
    _t(
        "Urban Fantasy", "subgenre", "magic hidden in the modern city", parent="Fantasy"
    ),
    _t(
        "Dark Fantasy",
        "subgenre",
        "fantasy with horror's teeth",
        parent="Fantasy",
        maturity=2,
    ),
    _t("Fairy Tale", "subgenre", "storybook logic and morals", parent="Fantasy"),
    _t(
        "Sword and Sorcery", "subgenre", "warriors, wizards, treasure", parent="Fantasy"
    ),
    _t(
        "Superhero",
        "subgenre",
        "powered heroes and their villains",
        ("comic book movie",),
        parent="Action",
    ),
    _t("Isekai", "subgenre", "transported into another world", parent="Fantasy"),
    _t(
        "Supernatural Drama",
        "subgenre",
        "paranormal forces outside horror",
        ("supernatural", "paranormal drama"),
        parent="Fantasy",
    ),
    _t("Wuxia", "subgenre", "Chinese martial chivalry", parent="Action"),
    _t(
        "Martial Arts",
        "subgenre",
        "hand-to-hand combat as craft",
        ("kung fu",),
        parent="Action",
    ),
    _t(
        "Samurai", "subgenre", "swordsmen bound by code", ("chanbara",), parent="Action"
    ),
    # --- comedy ---
    _t(
        "Romantic Comedy",
        "subgenre",
        "courtship played for laughs",
        ("romcom",),
        parent="Comedy",
    ),
    _t(
        "Workplace Comedy",
        "subgenre",
        "the job as ensemble stage",
        ("office comedy",),
        parent="Comedy",
    ),
    _t(
        "Sitcom",
        "subgenre",
        "recurring cast, self-contained episodes",
        ("situation comedy",),
        parent="Comedy",
        media=("tv",),
    ),
    _t(
        "Mockumentary",
        "subgenre",
        "fiction shot as documentary",
        ("mock doc",),
        parent="Comedy",
    ),
    _t("Screwball Comedy", "subgenre", "fast, verbal, farcical", parent="Comedy"),
    _t(
        "Dark Comedy",
        "subgenre",
        "grim subjects played funny",
        ("black comedy", "gallows humour"),
        parent="Comedy",
        maturity=2,
    ),
    _t(
        "Parody",
        "subgenre",
        "sends up another work or genre",
        ("spoof",),
        parent="Comedy",
    ),
    _t(
        "Stand-Up Special",
        "subgenre",
        "one comedian, one stage",
        ("standup",),
        parent="Comedy",
    ),
    _t("Buddy Comedy", "subgenre", "mismatched pair, forced together", parent="Comedy"),
    _t("Coming-of-Age Comedy", "subgenre", "growing up, played light", parent="Comedy"),
    _t(
        "Satire",
        "subgenre",
        "ridicule aimed at power",
        ("political satire",),
        parent="Comedy",
    ),
    _t("Slapstick", "subgenre", "physical comedy and pratfalls", parent="Comedy"),
    # --- drama ---
    _t(
        "Family Drama", "subgenre", "relatives, inheritance, old wounds", parent="Drama"
    ),
    _t("Medical Drama", "subgenre", "hospitals and the people in them", parent="Drama"),
    _t(
        "Emergency Services Drama",
        "subgenre",
        "firefighters, paramedics and rescue crews at work",
        ("first responders", "firefighter", "paramedic", "firehouse"),
        parent="Drama",
    ),
    _t("Sports Drama", "subgenre", "competition as the spine", parent="Drama"),
    _t(
        "Historical Drama",
        "subgenre",
        "the past taken seriously",
        ("period drama",),
        parent="Drama",
    ),
    _t(
        "Biographical Drama",
        "subgenre",
        "a real life dramatised",
        ("biopic",),
        parent="Drama",
    ),
    _t("Musical Biopic", "subgenre", "a musician's life and songs", parent="Drama"),
    _t("Courtroom Drama", "subgenre", "the trial is the story", parent="Drama"),
    _t("War Drama", "subgenre", "conflict through those who fight", parent="War"),
    _t(
        "Social Realism",
        "subgenre",
        "ordinary lives, unvarnished",
        ("kitchen sink",),
        parent="Drama",
    ),
    _t("Melodrama", "subgenre", "heightened emotion, big feeling", parent="Drama"),
    _t(
        "Slice of Life",
        "subgenre",
        "ordinary daily life without a grand plot",
        parent="Drama",
    ),
    _t("Teen Drama", "subgenre", "adolescence taken seriously", parent="Drama"),
    _t(
        "Coming-of-Age Drama", "subgenre", "growing up, played straight", parent="Drama"
    ),
    _t(
        "Disaster",
        "subgenre",
        "catastrophe and survival",
        ("disaster movie",),
        parent="Drama",
    ),
    _t(
        "Courtroom Procedural",
        "subgenre",
        "case-of-the-week legal work",
        parent="Drama",
        media=("tv",),
    ),
    _t(
        "Anthology Drama",
        "subgenre",
        "new story each season or episode",
        parent="Drama",
        media=("tv",),
    ),
    _t(
        "Soap Opera",
        "subgenre",
        "continuing serialised melodrama",
        parent="Drama",
        media=("tv",),
    ),
    # --- documentary / factual ---
    _t(
        "Nature Documentary",
        "subgenre",
        "wildlife and wild places",
        parent="Documentary",
    ),
    _t(
        "Music Documentary",
        "subgenre",
        "artists, scenes and tours",
        parent="Documentary",
    ),
    _t(
        "Sports Documentary",
        "subgenre",
        "athletes and their seasons",
        ("sports doc",),
        parent="Documentary",
    ),
    _t(
        "Docuseries",
        "subgenre",
        "multi-episode non-fiction",
        parent="Documentary",
        media=("tv",),
    ),
    _t(
        "Historical Documentary",
        "subgenre",
        "the past, evidenced",
        parent="Documentary",
    ),
    _t("Science Documentary", "subgenre", "research explained", parent="Documentary"),
    _t("Political Documentary", "subgenre", "power examined", parent="Documentary"),
    _t(
        "Food Documentary", "subgenre", "cooking, growing, eating", parent="Documentary"
    ),
    _t("Travel Documentary", "subgenre", "places, shown", parent="Documentary"),
    _t(
        "Reality Competition",
        "subgenre",
        "contestants eliminated over a season",
        ("competition reality", "elimination reality"),
        parent="Documentary",
        media=("tv",),
    ),
    _t(
        "Reality TV",
        "subgenre",
        "unscripted people and situations",
        ("reality", "reality show", "unscripted"),
        parent="Documentary",
        media=("tv",),
    ),
    _t(
        "Competition Show",
        "subgenre",
        "contestants compete for a prize",
        (
            "competition",
            "talent show",
            "singing competition",
            "cooking competition",
            "music competition",
            "challenge show",
        ),
        media=("tv",),
    ),
    _t(
        "Game Show",
        "subgenre",
        "studio games, questions and prizes",
        ("quiz show",),
        media=("tv",),
    ),
    _t(
        "Talk Show",
        "subgenre",
        "host and guests in conversation",
        ("talk", "interview", "celebrity interview"),
        media=("tv",),
    ),
    _t(
        "News Program",
        "subgenre",
        "current events and reporting",
        ("news", "current events"),
        media=("tv",),
    ),
    _t("Variety Show", "subgenre", "mixed music, comedy and guests", media=("tv",)),
    _t(
        "Cooking Show",
        "subgenre",
        "food prepared and explained",
        ("cooking", "food show"),
        media=("tv",),
    ),
    _t(
        "Travel Series",
        "subgenre",
        "places and cultures explored",
        ("travel show",),
        media=("tv",),
    ),
    _t(
        "Home Renovation Show",
        "subgenre",
        "homes redesigned or rebuilt",
        ("home renovation", "home improvement"),
        media=("tv",),
    ),
    _t(
        "Lifestyle Series",
        "subgenre",
        "everyday living, health and style",
        ("lifestyle",),
        media=("tv",),
    ),
    _t(
        "Music Program",
        "subgenre",
        "music, artists and performances",
        ("music show",),
        media=("tv",),
    ),
    _t(
        "Sports Program",
        "subgenre",
        "sporting events and analysis",
        ("sports show",),
        media=("tv",),
    ),
    _t(
        "Biographical Documentary",
        "subgenre",
        "a real life told factually",
        ("documentary biography",),
        parent="Documentary",
    ),
    _t(
        "Cultural Documentary",
        "subgenre",
        "a culture or community examined",
        parent="Documentary",
    ),
    _t(
        "Behind the Scenes",
        "subgenre",
        "the making of another work",
        ("behind-the-scenes", "making of", "filmmaking documentary"),
        parent="Documentary",
    ),
    _t(
        "Live Performance",
        "subgenre",
        "a staged performance captured live",
        ("performance film",),
    ),
    _t(
        "Nature Docuseries",
        "subgenre",
        "wildlife across episodes",
        parent="Documentary",
        media=("tv",),
    ),
    # --- other ---
    _t("Road Movie", "subgenre", "the journey is the plot", parent="Adventure"),
    _t(
        "Survival Adventure",
        "subgenre",
        "staying alive against nature",
        parent="Adventure",
    ),
    _t("Heist Comedy", "subgenre", "robbery with jokes", parent="Comedy"),
    _t("Christmas Movie", "subgenre", "built around the holiday", parent="Drama"),
    _t(
        "Concert Film",
        "subgenre",
        "a musical performance, filmed",
        ("live music", "filmed concert"),
        parent="Musical",
    ),
    _t(
        "Made for Television",
        "subgenre",
        "feature produced for broadcast",
        ("tv movie", "television movie"),
        media=("movie",),
    ),
    _t(
        "Experimental",
        "subgenre",
        "form over narrative",
        ("avant garde",),
        parent="Drama",
    ),
    _t("Silent Film", "subgenre", "no synchronised dialogue", parent="Drama"),
    _t("Short Film", "subgenre", "under forty minutes", parent="Drama"),
)

_MOOD: tuple[TagDef, ...] = (
    _t("Dark", "mood", "bleak tone, little comfort"),  # L
    _t("Gritty", "mood", "rough, unglamorous, street-level"),  # L
    _t("Feel-Good", "mood", "leaves you lifted", ("feelgood", "uplifting")),  # L
    _t("Quirky", "mood", "offbeat and idiosyncratic", ("offbeat", "oddball")),  # L
    _t("Suspenseful", "mood", "sustained uncertainty", ("suspense",)),  # L
    _t("Witty", "mood", "sharp, clever dialogue", ("clever",)),  # L
    _t("Heartwarming", "mood", "warm and affirming", ("wholesome",)),  # L
    _t("Intense", "mood", "relentless, high pressure"),  # L
    _t(
        "Cerebral", "mood", "rewards thinking", ("thought provoking", "intellectual")
    ),  # L
    _t("Lighthearted", "mood", "easy and untroubled"),  # L
    _t("Bittersweet", "mood", "joy and sorrow together"),
    _t("Bleak", "mood", "offers little hope", ("nihilistic",)),
    _t("Cozy", "mood", "comforting and low stakes", ("cosy", "comfort watch")),
    _t(
        "Absurdist",
        "mood",
        "logic deliberately abandoned",
        ("surreal comedy",),
    ),
    _t("Melancholy", "mood", "quietly sad throughout", ("wistful", "sad")),
    _t("Hopeful", "mood", "believes things improve", ("optimistic",)),
    _t("Tense", "mood", "tightly wound throughout"),
    _t("Cathartic", "mood", "builds to emotional release"),
    _t("Irreverent", "mood", "respects nothing", ("cheeky",)),
    _t("Steamy", "mood", "charged with desire", ("sexy", "sensual"), maturity=3),
    _t("Whimsical", "mood", "playful and fanciful"),
    _t("Haunting", "mood", "lingers after it ends"),
    _t("Nostalgic", "mood", "fond backward look"),
    _t("Rousing", "mood", "stirs and energises", ("inspirational", "inspiring")),
    _t(
        "Unsettling",
        "mood",
        "quietly wrong throughout",
        ("disquieting", "eerie"),
        maturity=2,
    ),
    _t("Tender", "mood", "gentle and affectionate"),
    _t("Brutal", "mood", "unsparing and harsh", maturity=3),
    _t("Playful", "mood", "light on its feet"),
    _t("Contemplative", "mood", "slow, reflective, quiet", ("meditative",)),
    _t("Raucous", "mood", "loud, rowdy, uninhibited", ("rowdy",)),
    _t("Romantic", "mood", "swept up in feeling"),
    _t("Sardonic", "mood", "dry and mocking", ("wry", "deadpan")),
    _t("Anxious", "mood", "keeps you on edge", ("nerve wracking",)),
    _t("Triumphant", "mood", "earns its victory"),
    _t("Chilling", "mood", "cold dread", ("creepy",), maturity=2),
)

_THEME: tuple[TagDef, ...] = (
    _t("Redemption", "theme", "earning a second chance"),
    _t("Grief", "theme", "living with loss", ("bereavement", "mourning")),
    _t(
        "Class Conflict", "theme", "rich against poor", ("class struggle", "inequality")
    ),
    _t("Found Family", "theme", "chosen bonds replace blood"),
    _t(
        "Corruption of Power",
        "theme",
        "authority rots those holding it",
        ("corruption",),
    ),
    _t("Immigrant Experience", "theme", "starting again elsewhere", ("immigration",)),
    _t("Faith and Doubt", "theme", "belief tested", ("religion", "faith")),
    _t(
        "Addiction and Recovery",
        "theme",
        "dependency and its way out",
        ("addiction", "sobriety"),
        maturity=2,
    ),
    _t("Generational Trauma", "theme", "damage inherited from parents"),
    _t("AI Ethics", "theme", "what machines owe us", ("artificial intelligence",)),
    _t("Identity", "theme", "working out who you are"),
    _t("Revenge and Its Cost", "theme", "vengeance takes something back"),
    _t("Coming of Age", "theme", "childhood ending", ("growing up",)),
    _t("Loyalty and Betrayal", "theme", "trust given and broken", ("betrayal",)),
    _t("Justice and the Law", "theme", "what is legal versus right", ("justice",)),
    _t("Ambition", "theme", "wanting more than is safe"),
    _t("Isolation", "theme", "cut off from others", ("loneliness",)),
    _t("Sacrifice", "theme", "giving up something that matters"),
    _t("Free Will", "theme", "whether choice is real", ("determinism",)),
    _t("Memory", "theme", "what we keep and lose"),
    _t("Mortality", "theme", "facing death", ("death",)),
    _t(
        "Parenthood",
        "theme",
        "raising and failing children",
        ("motherhood", "fatherhood", "parenting"),
    ),
    _t("Marriage", "theme", "partnership under strain"),
    _t("Friendship", "theme", "bonds outside family and romance"),
    _t("Coming Out", "theme", "claiming a queer identity", ("lgbtq", "queer")),
    _t("Race and Racism", "theme", "racial injustice examined", ("racism",)),
    _t("Gender Roles", "theme", "expectations by gender", ("feminism", "sexism")),
    _t("War's Aftermath", "theme", "what fighting leaves behind", ("ptsd", "veterans")),
    _t("Surveillance", "theme", "being watched", ("privacy",)),
    _t(
        "Environmental Collapse",
        "theme",
        "the planet failing",
        ("climate change", "ecology"),
    ),
    _t(
        "Capitalism",
        "theme",
        "markets and what they cost",
        ("greed", "capitalism critique"),
    ),
    _t("Technology and Society", "theme", "tools reshaping how we live"),
    _t("Colonialism", "theme", "empire and its subjects", ("imperialism",)),
    _t(
        "Mental Illness",
        "theme",
        "living with a disordered mind",
        ("mental health", "depression"),
        maturity=2,
    ),
    _t("Disability", "theme", "bodies and minds outside the norm"),
    _t("Poverty", "theme", "not having enough"),
    _t(
        "Religion and Cults",
        "theme",
        "belief organised into power",
        ("cult",),
        maturity=2,
    ),
    _t("Art and Creation", "theme", "making something, at a cost", ("artistry",)),
    _t("Fame", "theme", "being known and consumed", ("celebrity",)),
    _t("Small-Town Secrets", "theme", "everyone knows, nobody says"),
    _t("Corporate Malfeasance", "theme", "companies doing harm", ("corporate greed",)),
    _t("Political Idealism", "theme", "trying to change the system"),
    _t("Nature versus Nurture", "theme", "what makes a person"),
    _t("Legacy", "theme", "what outlasts you", ("inheritance",)),
    _t("Forgiveness", "theme", "letting a wrong go"),
    _t(
        "Social Commentary",
        "theme",
        "society and its failures examined",
        ("social issues", "social critique"),
    ),
    _t("Community", "theme", "people bound by a shared place"),
    _t("Activism", "theme", "organising to change society"),
    _t(
        "Environmentalism",
        "theme",
        "protecting nature from human damage",
        ("conservation",),
    ),
    _t(
        "Politics and Governance",
        "theme",
        "institutions, elections and public power",
        ("politics", "political", "governance"),
    ),
    _t("Culture and Tradition", "theme", "customs carried through generations"),
    _t("Trauma", "theme", "living after psychological injury"),
    _t("Obsession", "theme", "one pursuit consumes everything"),
)

_PLOT_DEVICE: tuple[TagDef, ...] = (
    _t("Heist", "plot_device", "planning and executing a robbery"),  # L
    _t(
        "Hostage Crisis",
        "plot_device",
        "captives held while rescuers or negotiators respond",
        ("hostage", "hijack", "hijacking", "hostage situation"),
    ),
    _t("Time Travel", "plot_device", "moving between periods"),  # L
    _t("Revenge", "plot_device", "settling a score", ("vengeance",)),  # L
    _t("Courtroom", "plot_device", "the case argued at trial", ("trial",)),  # L
    _t("Found Footage", "plot_device", "presented as recovered recordings"),  # L
    _t(
        "Coming-of-Age",
        "plot_device",
        "a young person grows up",
        display=False,
    ),  # L
    _t("Survival", "plot_device", "staying alive against the odds"),  # L
    _t("Conspiracy", "plot_device", "a hidden plot uncovered"),  # L
    _t(
        "Detective",
        "plot_device",
        "an investigator works the case",
        (
            "sleuth",
            "investigator",
            "investigation",
            "private investigator",
            "police detective",
            "murder investigation",
            "criminal investigation",
        ),
    ),  # L
    _t("Time Loop", "plot_device", "the same period repeats", ("groundhog day",)),
    _t("Amnesia", "plot_device", "a character has lost their memory", ("memory loss",)),
    _t("Undercover", "plot_device", "living inside the enemy", ("infiltration",)),
    _t("Locked Room", "plot_device", "an impossible confined crime"),
    _t(
        "Wrongful Conviction",
        "plot_device",
        "punished for what they didn't do",
        ("miscarriage of justice",),
    ),
    _t("Road Trip", "plot_device", "travel drives the story"),
    _t("Whodunit", "plot_device", "identify the culprit"),
    _t("Body Swap", "plot_device", "two people exchange bodies"),
    _t("Rescue Mission", "plot_device", "retrieve someone in danger"),
    _t("Fish Out of Water", "plot_device", "wrong person, wrong place"),
    _t("Prison Break", "plot_device", "escaping confinement", ("escape",)),
    _t("Rise and Fall", "plot_device", "built up then destroyed"),
    _t("Chosen One", "plot_device", "prophesied to matter"),
    _t("Mistaken Identity", "plot_device", "taken for someone else"),
    _t("Double Cross", "plot_device", "an ally turns", ("betrayal twist",)),
    _t("Ticking Clock", "plot_device", "a hard deadline", ("race against time",)),
    _t("One Last Job", "plot_device", "retiring after this one"),
    _t("Cover-Up", "plot_device", "hiding what happened"),
    _t("Manhunt", "plot_device", "pursuing a fugitive", ("fugitive",)),
    _t("Kidnapping", "plot_device", "someone is taken", ("abduction",), maturity=2),
    _t("Sole Survivor", "plot_device", "one lives through it"),
    _t(
        "Twist Ending",
        "plot_device",
        "the final reveal recontextualises",
        ("plot twist",),
    ),
    _t(
        "Nonlinear Timeline",
        "plot_device",
        "events out of order",
        ("nonlinear", "fractured narrative"),
    ),
    _t(
        "Framed Narrative",
        "plot_device",
        "a story inside a story",
        ("frame story", "story within a story"),
    ),
    _t("Heist Gone Wrong", "plot_device", "the plan fails"),
    _t(
        "Trapped Together",
        "plot_device",
        "confined with each other",
        ("bottle episode", "confined"),
    ),
    _t("Secret Identity", "plot_device", "living two lives", ("double life",)),
    _t("Redemption Arc", "plot_device", "a bad character turns good"),
    _t("Slow Reveal", "plot_device", "truth doled out gradually"),
    _t("Unreliable Account", "plot_device", "the telling cannot be trusted"),
    _t(
        "Forbidden Romance", "plot_device", "love that isn't allowed", ("star crossed",)
    ),
    _t("Love Triangle", "plot_device", "three-way romantic tangle"),
    _t("Missing Person", "plot_device", "someone has vanished", ("disappearance",)),
    _t("Serial Killer", "plot_device", "a repeat murderer hunted", maturity=3),
    _t("Cult Infiltration", "plot_device", "getting inside a closed group", maturity=2),
    _t("Apocalypse Event", "plot_device", "the world ends on screen"),
    _t("Alien Contact", "plot_device", "meeting non-human intelligence"),
    # Named "Ghostly Presence" rather than "Haunting" so it cannot collide with
    # the mood tag of that name; a bare "haunting" from the model resolves to
    # the mood, which is the commoner reading.
    _t("Ghostly Presence", "plot_device", "a presence will not leave", maturity=2),
    _t("Possession", "plot_device", "another will takes a body", maturity=3),
    _t("Tournament", "plot_device", "structured elimination contest"),
    _t("Training Montage", "plot_device", "skills built to a beat"),
    _t("Courtroom Reversal", "plot_device", "the case turns on evidence"),
    _t("Deal with the Devil", "plot_device", "power bought at a price"),
)

_SETTING_PERIOD: tuple[TagDef, ...] = (
    _t("Ancient World", "setting_period", "before the fall of Rome", ("antiquity",)),
    _t("Ancient Rome", "setting_period", "the Roman republic or empire"),
    _t("Medieval", "setting_period", "the European middle ages", ("middle ages",)),
    _t("Renaissance", "setting_period", "early modern Europe"),
    _t("Feudal Japan", "setting_period", "shoguns, samurai, domains", ("edo period",)),
    _t("Age of Sail", "setting_period", "tall ships and empire", ("pirates",)),
    _t("Victorian Era", "setting_period", "nineteenth century Britain", ("victorian",)),
    _t("Wild West Era", "setting_period", "the American frontier years", ("old west",)),
    _t("Belle Epoque", "setting_period", "turn of the twentieth century"),
    _t("World War I", "setting_period", "the first world war", ("ww1", "great war")),
    _t(
        "Prohibition",
        "setting_period",
        "American nineteen-twenties bootlegging",
        ("roaring twenties",),
    ),
    _t("Great Depression", "setting_period", "the nineteen-thirties slump"),
    _t("World War II", "setting_period", "the second world war", ("ww2", "wwii")),
    _t("Cold War", "setting_period", "east against west", ("iron curtain",)),
    _t("1960s Setting", "setting_period", "story set in the sixties"),
    _t("1970s Setting", "setting_period", "story set in the seventies"),
    _t("1980s Setting", "setting_period", "story set in the eighties"),
    _t("1990s Setting", "setting_period", "story set in the nineties"),
    _t("Present Day", "setting_period", "contemporary setting", ("contemporary",)),
    _t("Near Future", "setting_period", "recognisably soon"),
    _t("Far Future", "setting_period", "centuries ahead"),
    _t("Prehistoric", "setting_period", "before written record"),
    _t("Alternate History", "setting_period", "the past went differently"),
    _t("Post-War Years", "setting_period", "rebuilding after conflict"),
    _t(
        "Turn of the Millennium",
        "setting_period",
        "around the year two thousand",
        ("y2k",),
    ),
)

_PLACE: tuple[TagDef, ...] = (
    _t("Small-Town America", "place", "population in the thousands", ("small town",)),
    _t("New York City", "place", "the five boroughs", ("nyc", "manhattan")),
    # ``la`` is not a safe retrieval alias: job 225 matched the article in
    # "Ernesto de la Cruz" and tagged Coco as Los Angeles. Keep the useful
    # human-facing alias in prose while requiring an unambiguous phrase.
    _t("Los Angeles", "place", "southern California sprawl", ("hollywood",)),
    _t("London", "place", "the British capital"),
    _t("Paris", "place", "the French capital"),
    _t("Tokyo", "place", "the Japanese capital"),
    _t("Seoul", "place", "the South Korean capital"),
    _t("Rural England", "place", "English villages and countryside"),
    _t("The Scottish Highlands", "place", "northern Scotland", ("highlands",)),
    _t(
        "Outer Space",
        "place",
        "beyond the atmosphere",
        ("space", "space travel", "spacecraft", "spaceship"),
    ),
    _t("Space Station", "place", "a crewed orbital habitat"),
    _t("Another Planet", "place", "a world that is not Earth"),
    _t("Suburbia", "place", "commuter-belt housing", ("suburbs",)),
    _t("High School", "place", "secondary school as setting"),
    _t("College Campus", "place", "university life", ("university",)),
    _t("Hospital", "place", "wards, theatres, emergency rooms"),
    _t("Prison", "place", "inside a correctional facility"),
    _t("Courtroom Setting", "place", "the trial chamber"),
    _t("The Desert", "place", "arid emptiness", ("desert", "desert planet")),
    _t("The Arctic", "place", "ice and permanent cold", ("antarctic", "polar")),
    _t("Jungle", "place", "dense tropical forest", ("rainforest",)),
    _t("At Sea", "place", "aboard a ship or adrift", ("ocean", "aboard a ship")),
    _t("Underwater", "place", "beneath the surface"),
    _t("Mountains", "place", "high altitude and climbing"),
    _t("Island", "place", "cut off by water"),
    _t("The Road", "place", "highways and motels"),
    _t("Wall Street", "place", "high finance", ("finance", "banking")),
    _t("Newsroom", "place", "journalism at work", ("journalism",)),
    _t("Military Base", "place", "life in barracks and command"),
    _t("The Vatican", "place", "the seat of the church"),
    _t("Las Vegas", "place", "casinos and the strip", ("vegas",)),
    _t("Post-Soviet States", "place", "the former eastern bloc"),
)

# NOTE: origin tags are mostly `derived` -- computed from original_language and
# production_countries rather than asked of the model. All 12 legacy region tags
# are canonical here.
_ORIGIN: tuple[TagDef, ...] = (
    _t(
        "American", "origin", "made in the United States", ("usa", "us"), derived=True
    ),  # L
    _t(
        "British",
        "origin",
        "made in the United Kingdom",
        ("uk", "english"),
        derived=True,
    ),  # L
    _t(
        "Korean", "origin", "made in Korea", ("k drama", "south korean"), derived=True
    ),  # L
    _t("Japanese", "origin", "made in Japan", derived=True),  # L
    _t("French", "origin", "made in France", derived=True),  # L
    _t("Italian", "origin", "made in Italy", derived=True),  # L
    _t("Spanish", "origin", "made in Spain", derived=True),  # L
    _t("Chinese", "origin", "made in China", ("mandarin",), derived=True),  # L
    _t("Indian", "origin", "made in India", derived=True),  # L
    _t(
        "Scandinavian",
        "origin",
        "made in the Nordic countries",
        ("nordic", "nordic noir country"),
        derived=True,
    ),  # L
    _t(
        "Latin American",
        "origin",
        "made in Latin America",
        ("latino", "latin america"),
        derived=True,
    ),  # L
    _t("European", "origin", "made in Europe", derived=True),  # L
    _t("German", "origin", "made in Germany", derived=True),
    _t("Australian", "origin", "made in Australia", ("aussie",), derived=True),
    _t("Canadian", "origin", "made in Canada", derived=True),
    _t("Irish", "origin", "made in Ireland", derived=True),
    _t("Mexican", "origin", "made in Mexico", derived=True),
    _t("Brazilian", "origin", "made in Brazil", derived=True),
    _t("Argentine", "origin", "made in Argentina", ("argentinian",), derived=True),
    _t("Russian", "origin", "made in Russia", derived=True),
    _t("Polish", "origin", "made in Poland", derived=True),
    _t("Turkish", "origin", "made in Turkey", derived=True),
    _t("Israeli", "origin", "made in Israel", derived=True),
    _t("Iranian", "origin", "made in Iran", ("persian",), derived=True),
    _t("Nigerian", "origin", "made in Nigeria", ("nollywood",), derived=True),
    _t("Thai", "origin", "made in Thailand", derived=True),
    _t("Filipino", "origin", "made in the Philippines", derived=True),
    _t("Hong Kong", "origin", "made in Hong Kong", ("hk",), derived=True),
    _t("Taiwanese", "origin", "made in Taiwan", derived=True),
    # Not derivable from country/language alone -- the model assigns these.
    _t("Anime", "origin", "Japanese animation tradition", ("japanese animation",)),
    _t("Bollywood", "origin", "Hindi-language commercial cinema", parent="Indian"),
    _t(
        "Nordic Noir", "origin", "bleak Scandinavian crime style", parent="Scandinavian"
    ),
)

_STYLE: tuple[TagDef, ...] = (
    _t(
        "Arthouse", "style", "auteur-driven, non-commercial", ("art house", "art film")
    ),  # L
    _t("Independent", "style", "made outside the studio system", ("indie",)),  # L
    _t(
        "Visually Stunning",
        "style",
        "exceptional images",
        ("beautiful cinematography",),
    ),  # L
    _t("Minimalist", "style", "stripped to essentials", ("spare", "austere")),  # L
    _t(
        "Period Costume", "style", "elaborate historical dress", ("costume drama",)
    ),  # L
    _t(
        "Neon Visuals", "style", "saturated neon palette", ("neon soaked", "neon lit")
    ),  # L
    _t("Satirical", "style", "mocking through exaggeration"),  # L
    _t(
        "Handheld Verite",
        "style",
        "documentary-style camerawork",
        ("handheld", "cinema verite", "shaky cam"),
    ),
    _t(
        "Long-Take",
        "style",
        "extended unbroken shots",
        ("oner", "one take", "long take"),
    ),
    _t(
        "Black and White",
        "style",
        "monochrome photography",
        ("monochrome", "b&w", "black white"),
    ),
    _t(
        "Stop-Motion", "style", "animated frame by frame", ("stop motion", "claymation")
    ),
    _t(
        "Cel Animation",
        "style",
        "traditional hand-drawn animation",
        ("hand drawn", "2d animation"),
    ),
    _t(
        "CG Animation", "style", "computer-generated animation", ("cgi", "3d animation")
    ),
    _t(
        "Practical Effects",
        "style",
        "physical rather than digital effects",
        ("practical fx", "no cgi"),
    ),
    _t(
        "Needle-Drop Soundtrack",
        "style",
        "existing songs used pointedly",
        ("great soundtrack", "needle drop"),
    ),
    _t("Synth Score", "style", "synthesiser-driven music", ("synthwave", "synth")),
    _t("Orchestral Score", "style", "sweeping orchestral music"),
    _t(
        "Voiceover Narration",
        "style",
        "a narrator guides the story",
        ("narration", "voice over"),
    ),
    _t(
        "Breaking the Fourth Wall",
        "style",
        "addresses the audience directly",
        ("fourth wall",),
    ),
    _t("Slow Cinema", "style", "long, quiet, unhurried"),
    _t("Maximalist", "style", "excess as an aesthetic", ("baroque", "over the top")),
    _t("Gritty Realism", "style", "unpolished and documentary-like", ("realist",)),
    _t("Stylised Violence", "style", "choreographed and aestheticised", maturity=3),
    _t("Dreamlike", "style", "logic of a dream", ("surreal", "oneiric")),
    _t(
        "Retro Aesthetic",
        "style",
        "deliberately of another decade",
        ("retro", "vintage"),
    ),
    _t(
        "Found-Object Editing",
        "style",
        "archive and collage",
        ("archival footage", "collage"),
    ),
    _t("Single Location", "style", "confined to one place", ("one location",)),
    _t(
        "Ensemble Structure",
        "style",
        "many parallel storylines",
        ("hyperlink", "multiple storylines"),
    ),
    _t(
        "Anthology Structure",
        "style",
        "separate stories in one work",
        display=False,
    ),
    _t("Mockumentary Style", "style", "shot as if factual", display=False),
    _t(
        "Animated for Adults",
        "style",
        "animation with adult subject matter",
        display=False,
        maturity=3,
    ),
    _t("Puppetry", "style", "performed with puppets"),
    _t("Rotoscope", "style", "animation traced over live action"),
    _t(
        "Widescreen Spectacle",
        "style",
        "built for the biggest screen",
        ("imax", "epic scale"),
    ),
    _t(
        "Naturalistic Dialogue",
        "style",
        "overlapping, unpolished speech",
        ("improvised", "improvisational"),
    ),
)

_CHARACTER: tuple[TagDef, ...] = (
    _t(
        "Strong Female Lead",
        "character",
        "a woman drives the story",
        ("female lead", "female protagonist"),
    ),  # L
    _t("Anti-Hero", "character", "protagonist with bad methods", ("antihero",)),  # L
    _t("Ensemble Cast", "character", "no single lead", ("ensemble cast movie",)),  # L
    _t("Character Study", "character", "one person examined closely"),  # L
    _t("Underdog", "character", "outmatched and unfavoured"),  # L
    _t(
        "Morally Ambiguous",
        "character",
        "hard to call good or bad",
        ("morally grey", "moral ambiguity"),
    ),  # L
    _t("Reluctant Hero", "character", "did not want the job"),  # L
    # Legacy "Ensemble" is preserved as a canonical name for R1, but hidden as a
    # chip because it duplicates "Ensemble Cast".
    _t(
        "Ensemble", "character", "no single lead", parent="Ensemble Cast", display=False
    ),  # L
    _t("Unreliable Narrator", "character", "the teller misleads us"),
    _t("Genius Detective", "character", "solves what others cannot"),
    _t("Working-Class Hero", "character", "ordinary person, extraordinary pressure"),
    _t("Mentor Figure", "character", "teaches the protagonist", ("mentor",)),
    _t("Villain Protagonist", "character", "we follow the bad one"),
    _t("Reluctant Leader", "character", "command they did not seek"),
    _t("Chosen Family", "character", "bonds outside blood"),
    _t("Odd Couple", "character", "mismatched pair bound together"),
    _t("Femme Fatale", "character", "dangerous and alluring", maturity=2),
    _t("Loner", "character", "operates apart from others", ("lone wolf",)),
    _t("Precocious Child", "character", "a child beyond their years"),
    _t(
        "Ageing Protagonist",
        "character",
        "late life at the centre",
        ("elderly protagonist",),
    ),
    _t("Queer Lead", "character", "an LGBTQ+ protagonist", ("lgbtq lead", "gay lead")),
    _t("Disabled Lead", "character", "a disabled protagonist"),
    _t("Large Family", "character", "many relatives in play"),
    _t("Rivals", "character", "two matched opponents", ("rivalry",)),
    _t("Partners in Crime", "character", "criminal double act"),
    _t("Found Mentor", "character", "guidance from an unlikely source"),
    _t("Redeemed Villain", "character", "the antagonist turns"),
    _t("Everyman", "character", "deliberately ordinary lead"),
    _t("Prodigy", "character", "exceptional talent, young"),
    _t("Survivor Protagonist", "character", "defined by what they lived through"),
    _t("Twin Leads", "character", "two protagonists, equal weight", ("dual leads",)),
)

_AUDIENCE: tuple[TagDef, ...] = (
    _t("Kids", "audience", "made for young children", ("children", "kids tv")),
    _t("Family Movie Night", "audience", "works for all ages together"),
    _t("Teen", "audience", "aimed at adolescents", ("young adult", "ya")),
    _t("Adult Animation", "audience", "animation not for children", maturity=3),
    _t("Date Night", "audience", "good for watching as a couple"),
    _t(
        "Background Watch",
        "audience",
        "forgiving of divided attention",
        ("easy watching",),
    ),
    _t("Water-Cooler Show", "audience", "everyone is discussing it", media=("tv",)),
    _t("Comfort Rewatch", "audience", "rewards repeat viewing", ("rewatchable",)),
    _t("Film Buff", "audience", "rewards cinema literacy", ("cinephile",)),
    _t("Group Watch", "audience", "better with a crowd", ("party movie",)),
    _t("Solo Viewing", "audience", "demands undivided attention"),
    _t("Late Night", "audience", "suits the small hours"),
    _t("Educational", "audience", "made to teach", ("learning",)),
    _t("Preschool", "audience", "for the very young", parent="Kids"),
    _t("All Ages", "audience", "no content barrier at any age"),
)

_MATURITY: tuple[TagDef, ...] = (
    _t(
        "Gore",
        "maturity",
        "graphic blood and injury",
        ("graphic violence",),
        display=False,
        maturity=4,
    ),
    _t(
        "Sexual Content",
        "maturity",
        "explicit sexual material",
        ("nudity", "sex scenes"),
        display=False,
        maturity=4,
    ),
    _t(
        "Strong Language",
        "maturity",
        "pervasive profanity",
        ("profanity", "swearing"),
        display=False,
        maturity=3,
    ),
    _t(
        "Drug Use",
        "maturity",
        "depicted narcotics use",
        ("drugs",),
        display=False,
        maturity=3,
    ),
    _t(
        "Disturbing Imagery",
        "maturity",
        "images meant to distress",
        display=False,
        maturity=3,
    ),
    _t(
        "Self-Harm",
        "maturity",
        "depicts self-injury or suicide",
        ("suicide",),
        display=False,
        maturity=4,
    ),
    _t(
        "Sexual Violence",
        "maturity",
        "depicts sexual assault",
        ("rape",),
        display=False,
        maturity=4,
    ),
    _t("Animal Harm", "maturity", "animals hurt on screen", display=False, maturity=3),
    _t(
        "Child Endangerment",
        "maturity",
        "children placed in danger",
        display=False,
        maturity=3,
    ),
    _t(
        "Flashing Imagery",
        "maturity",
        "strobing that may trigger seizures",
        ("strobe",),
        display=False,
        maturity=0,
    ),
)

_PACING_FORMAT: tuple[TagDef, ...] = (
    _t(
        "Slow-Burn",
        "pacing_format",
        "builds slowly and deliberately",
        ("slow burn", "slowburn"),
    ),  # L
    _t(
        "Fast-Paced",
        "pacing_format",
        "moves quickly throughout",
        ("fast paced", "brisk"),
    ),  # L
    _t("Breakneck", "pacing_format", "relentless forward momentum", ("frenetic",)),
    _t(
        "Bingeable",
        "pacing_format",
        "hard to stop watching",
        ("binge worthy",),
        media=("tv",),
    ),
    _t(
        "Episodic",
        "pacing_format",
        "self-contained episodes",
        ("case of the week",),
        media=("tv",),
    ),
    _t(
        "Serialized",
        "pacing_format",
        "one story across the season",
        ("serialised",),
        media=("tv",),
    ),
    _t("Anthology", "pacing_format", "new story each season or episode", media=("tv",)),
    _t("Meditative Pace", "pacing_format", "unhurried and still"),
    _t("Twisty", "pacing_format", "frequent reversals"),
    _t(
        "Limited Series",
        "pacing_format",
        "one short closed season",
        ("miniseries", "mini series"),
        media=("tv",),
        derived=True,
    ),
    _t(
        "Multi-Season Saga",
        "pacing_format",
        "four or more seasons",
        media=("tv",),
        derived=True,
    ),
    _t(
        "Under 90 Minutes",
        "pacing_format",
        "short runtime",
        ("short runtime",),
        media=("movie",),
        derived=True,
    ),
    _t(
        "Epic Runtime",
        "pacing_format",
        "two and a half hours or more",
        ("long movie",),
        media=("movie",),
        derived=True,
    ),
    _t(
        "Feature Length",
        "pacing_format",
        "standard feature runtime",
        media=("movie",),
        derived=True,
        display=False,
    ),
    _t(
        "Single Season",
        "pacing_format",
        "exactly one season",
        media=("tv",),
        derived=True,
        display=False,
    ),
    _t("Slow Start", "pacing_format", "takes time to find itself"),
)

_SOURCE_FRANCHISE: tuple[TagDef, ...] = (
    _t(
        "Based on a Book",
        "source_franchise",
        "adapted from a novel",
        (
            "book adaptation",
            "novel adaptation",
            "based on novel",
            "based on a novel",
            "based on novel or book",
        ),
    ),
    _t(
        "Based on a True Story",
        "source_franchise",
        "drawn from real events",
        ("true story", "based on real events"),
    ),
    _t(
        "Comic Book Adaptation",
        "source_franchise",
        "adapted from comics",
        ("graphic novel", "comic adaptation", "based on comic"),
    ),
    _t(
        "Video Game Adaptation",
        "source_franchise",
        "adapted from a game",
        ("game adaptation",),
    ),
    _t(
        "Stage Adaptation",
        "source_franchise",
        "adapted from theatre",
        ("play adaptation", "based on a play"),
    ),
    _t("Remake", "source_franchise", "a new version of an older work"),
    _t("Sequel", "source_franchise", "continues an earlier film"),
    _t("Prequel", "source_franchise", "set before an earlier film"),
    _t("Reboot", "source_franchise", "restarts a franchise"),
    _t("Spin-Off", "source_franchise", "branches from another work", ("spinoff",)),
    _t(
        "Cinematic Universe",
        "source_franchise",
        "part of a shared world",
        ("shared universe",),
    ),
    _t(
        "Manga Adaptation",
        "source_franchise",
        "adapted from manga",
        ("based on manga",),
    ),
    _t("Podcast Adaptation", "source_franchise", "adapted from a podcast"),
)

_SEASONAL: tuple[TagDef, ...] = (
    _t("Christmas", "seasonal", "set at or about Christmas", ("xmas",)),
    _t("Halloween", "seasonal", "set at or about Halloween"),
    _t("Valentine's", "seasonal", "romance for the fourteenth", ("valentines",)),
    _t("Thanksgiving", "seasonal", "set at Thanksgiving"),
    _t("New Year", "seasonal", "set at the turn of the year"),
    _t("Summer Blockbuster", "seasonal", "big-budget summer release", ("blockbuster",)),
    _t("Awards Season", "seasonal", "released for awards contention"),
    _t("Back to School", "seasonal", "the school year beginning"),
)

_PEDIGREE: tuple[TagDef, ...] = (
    _t("Oscar Winner", "pedigree", "won an Academy Award", ("academy award",)),
    _t("Oscar Nominated", "pedigree", "nominated for an Academy Award"),
    _t("Palme d'Or", "pedigree", "top prize at Cannes", ("cannes winner", "palme dor")),
    _t(
        "Festival Favorite",
        "pedigree",
        "acclaimed on the festival circuit",
        ("festival favourite", "sundance"),
    ),
    _t("Cult Classic", "pedigree", "small devoted following", ("cult film",)),
    _t(
        "Critics' Darling",
        "pedigree",
        "strongly reviewed",
        ("critically acclaimed", "critics darling"),
    ),
    _t("Box Office Hit", "pedigree", "major commercial success", ("blockbuster hit",)),
    _t("Emmy Winner", "pedigree", "won an Emmy", media=("tv",)),
    _t(
        "Debut Feature", "pedigree", "the director's first film", ("directorial debut",)
    ),
    _t("Hidden Gem", "pedigree", "well made and little seen", ("underrated",)),
)

_RELEASE_DECADE: tuple[TagDef, ...] = tuple(
    _t(f"{d}s", "release_decade", f"released in the {d}s", derived=True, sort_hint=900)
    for d in range(1920, 2030, 10)
)  # L x11 (1920s..2020s)

TAXONOMY: tuple[TagDef, ...] = (
    _SUBGENRE
    + _MOOD
    + _THEME
    + _PLOT_DEVICE
    + _SETTING_PERIOD
    + _PLACE
    + _ORIGIN
    + _STYLE
    + _CHARACTER
    + _AUDIENCE
    + _MATURITY
    + _PACING_FORMAT
    + _SOURCE_FRANCHISE
    + _SEASONAL
    + _PEDIGREE
    + _RELEASE_DECADE
)


# ---------------------------------------------------------------------------
# Indexes
# ---------------------------------------------------------------------------
BY_NAME: dict[str, TagDef] = {t.name: t for t in TAXONOMY}
BY_FACET: dict[str, tuple[TagDef, ...]] = {
    facet: tuple(t for t in TAXONOMY if t.facet == facet) for facet in FACETS
}

#: normalized string -> canonical tag name. Covers canonical names themselves so
#: a single lookup resolves both.
ALIAS_INDEX: dict[str, str] = {}
for _tag in TAXONOMY:
    ALIAS_INDEX.setdefault(normalize(_tag.name), _tag.name)
for _tag in TAXONOMY:
    for _alias in _tag.aliases:
        ALIAS_INDEX.setdefault(normalize(_alias), _tag.name)
del _tag

#: No facet is offered wholesale. The previous four-facet core contained 91
#: choices before title evidence was considered and forced the shortlister to
#: pad unrelated large facets just to reach a floor. A compact judgement core
#: plus evidence retrieval gives Gemini enough expressive range without making
#: every plausible-sounding concept legal for every title.
ALWAYS_OFFERED_FACETS: tuple[str, ...] = ()

#: Concepts that can be inferred from a synopsis even when TMDB has no matching
#: keyword. Specific plot devices, places, periods, adaptations and technical
#: styles are intentionally absent: they must be supported by title evidence.
# Version 5 has no unconditional model choices. Jobs 221-227 demonstrated that
# even a compact mood menu becomes filler when a small model sees a title with
# few interpretive signals. Every model candidate must now be retrieved from
# the title synopsis or trusted TMDB facts, and returning zero is valid.
CORE_OFFERED_NAMES: frozenset[str] = frozenset()

# These labels have repeatedly become generic filler or have inverted the tone
# of a title. They remain supported concepts, but are offered only when the
# title, synopsis or factual keywords retrieve them. Reviews never count as
# evidence: job 226 called Chucky Heartwarming and Zootopia 2 Bleak by copying
# incidental reviewer wording.
EVIDENCE_ONLY_MOOD_NAMES: frozenset[str] = frozenset(
    {
        "Cerebral",
        "Contemplative",
        "Bleak",
        "Cozy",
        "Heartwarming",
        "Tender",
        "Chilling",
    }
)

# TV format labels are especially easy to guess from the mere fact that a title
# is a series. ``Anthology`` was assigned to 205/821 pilot shows even though
# only two exports mentioned anthology structure. All four now require direct
# title evidence instead of being legal defaults.
TV_CORE_OFFERED_NAMES: frozenset[str] = frozenset()

#: Legacy concepts retained for database compatibility but no longer presented
#: as separate model choices. Validation canonicalizes old spellings for direct
#: callers; new assignments use one concept and therefore cannot double-weight
#: a row.
PROMPT_SUPPRESSED_NAMES: frozenset[str] = frozenset(
    {
        "Coming-of-Age",
        "Ensemble",
        "Anthology Structure",
        "Mockumentary Style",
        "Animated for Adults",
        "Remake",
    }
)

#: TMDB genres are authoritative broad facts. Pinning these first prevents the
#: shortlister from dropping defining genres such as Crime/Drama while filling
#: the menu with weak setting or adaptation guesses.
TMDB_GENRE_TAGS: dict[str, tuple[str, ...]] = {
    normalize("Action"): ("Action",),
    normalize("Adventure"): ("Adventure",),
    normalize("Animation"): ("Animation",),
    normalize("Comedy"): ("Comedy",),
    normalize("Crime"): ("Crime",),
    normalize("Documentary"): ("Documentary",),
    normalize("Drama"): ("Drama",),
    normalize("Family"): ("Family", "Family Movie Night"),
    normalize("Fantasy"): ("Fantasy",),
    normalize("History"): ("History",),
    normalize("Horror"): ("Horror",),
    normalize("Music"): ("Music",),
    normalize("Mystery"): ("Mystery",),
    normalize("Romance"): ("Romance",),
    normalize("Science Fiction"): ("Sci-Fi",),
    normalize("TV Movie"): ("Made for Television",),
    normalize("Thriller"): ("Thriller",),
    normalize("War"): ("War",),
    normalize("Western"): ("Western",),
    normalize("Action & Adventure"): ("Action", "Adventure"),
    normalize("Kids"): ("Kids",),
    normalize("News"): ("News Program",),
    normalize("Reality"): ("Reality TV",),
    normalize("Sci-Fi & Fantasy"): ("Sci-Fi", "Fantasy"),
    normalize("Soap"): ("Soap Opera",),
    normalize("Talk"): ("Talk Show",),
    normalize("War & Politics"): ("War", "Political Drama"),
}

# TMDB combines two materially different TV genres into one value. Treating
# that value as two authoritative facts produced Sci-Fi + Fantasy on 225 pilot
# titles, including Sci-Fi on Game of Thrones. The combined value is only a
# candidate menu; a side becomes a derived fact when independent title evidence
# supports it. With no evidence the model may still choose from both.
_AMBIGUOUS_TMDB_GENRE_SIGNALS: dict[str, dict[str, tuple[str, ...]]] = {
    normalize("Sci-Fi & Fantasy"): {
        "Sci-Fi": (
            "science fiction",
            "sci fi",
            "space",
            "spaceship",
            "alien",
            "extraterrestrial",
            "robot",
            "android",
            "artificial intelligence",
            "time travel",
            "futuristic",
            "cyberpunk",
            "dystopian",
        ),
        "Fantasy": (
            "fantasy",
            "magic",
            "magical",
            "wizard",
            "witch",
            "dragon",
            "mythical",
            "mythology",
            "supernatural",
            "demon",
            "vampire",
            "fairy",
            "elf",
            "reincarnation",
            "cultivation",
            "isekai",
            "spell",
        ),
    },
    normalize("War & Politics"): {
        "War": (
            "war",
            "military",
            "army",
            "battlefield",
            "soldier",
            "navy",
            "combat",
        ),
        "Political Drama": (
            "politics",
            "political",
            "government",
            "president",
            "parliament",
            "election",
            "diplomacy",
            "senator",
        ),
    },
}


def _genre_disambiguation_text(features: dict[str, Any]) -> str:
    """Evidence for a combined TMDB genre, deliberately excluding genres."""
    values: list[Any] = [features.get("title"), features.get("overview")]
    for source in (
        features,
        features.get("tag_facts") or {},
        features.get("mdb_facts") or {},
    ):
        if not isinstance(source, dict):
            continue
        for key in ("keywords",):
            value = source.get(key)
            if isinstance(value, (list, tuple)):
                values.extend(value)
            elif value:
                values.append(value)
    return f" {normalize(' '.join(str(value) for value in values if value))} "


def _tmdb_genre_names(
    features: dict[str, Any], genre: Any, *, derived: bool
) -> tuple[str, ...]:
    key = normalize(genre)
    names = TMDB_GENRE_TAGS.get(key, ())
    signals = _AMBIGUOUS_TMDB_GENRE_SIGNALS.get(key)
    if not signals:
        return names

    text = _genre_disambiguation_text(features)
    supported = tuple(
        name
        for name, phrases in signals.items()
        if any(f" {normalize(phrase)} " in text for phrase in phrases)
    )
    # Shortlisting remains recall-oriented: when the metadata is genuinely
    # ambiguous, offer both and let the model decide. Derivation is factual and
    # therefore emits nothing without independent evidence.
    return supported or (() if derived else names)


TMDB_PINNED_TAG_NAMES: frozenset[str] = frozenset(
    name for names in TMDB_GENRE_TAGS.values() for name in names
)

#: Every non-derived facet can be evidence-retrieved. The strict per-facet caps
#: prevent a keyword-rich synopsis from spending the whole legal menu on one
#: dimension.
RETRIEVED_FACETS: tuple[str, ...] = tuple(
    facet for facet in FACETS if facet != "release_decade"
)

SHORTLIST_MAX = 90
RETRIEVED_PER_FACET = 6
FLOOR_PER_FACET = 0
FACET_RETRIEVAL_LIMITS: dict[str, int] = {
    "subgenre": 12,
    "mood": 6,
    "theme": 8,
    "plot_device": 6,
    "style": 6,
    "character": 5,
    "audience": 4,
    "pacing_format": 5,
    "place": 4,
    "setting_period": 4,
    "source_franchise": 5,
    "seasonal": 3,
    "origin": 3,
    "maturity": 5,
    "pedigree": 4,
}
FACET_FLOORS: dict[str, int] = {facet: 0 for facet in RETRIEVED_FACETS}

# A generic gloss-token overlap is useful for themes, but unsafe for factual
# labels. Before this boundary, the words "world" and "war" made World War I a
# legal choice for Schindler's List, while the word "animation" made both Cel
# and CG techniques legal. These facets now require the canonical phrase or an
# explicit alias in the title evidence.
STRICT_EVIDENCE_FACETS: frozenset[str] = frozenset(
    {
        "setting_period",
        "style",
        "source_franchise",
        "pedigree",
        "maturity",
        "place",
        "origin",
        "audience",
    }
)

# These labels describe an unscripted television format, not merely a subject
# inside a fictional story. Job 223 otherwise turned a restaurant comedy into
# a Cooking Show, an animated journey into a Travel Series, and a fantasy
# tournament into a Competition Show. A matching unscripted TMDB genre or an
# explicit multiword format phrase is required below.
UNSCRIPTED_FORMAT_TAG_NAMES: frozenset[str] = frozenset(
    {
        "Reality Competition",
        "Reality TV",
        "Competition Show",
        "Game Show",
        "Talk Show",
        "News Program",
        "Variety Show",
        "Cooking Show",
        "Travel Series",
        "Home Renovation Show",
        "Lifestyle Series",
        "Sports Program",
    }
)
UNSCRIPTED_GENRE_LABELS: frozenset[str] = frozenset(
    normalize(value) for value in ("Documentary", "Reality", "Talk", "News")
)

# Always offering a compact mood palette lets the model judge tone even when a
# synopsis does not literally say "cozy" or "bleak". Nano models still need a
# broad-genre safety rail: job 223 called Game of Thrones Cozy/Hopeful and
# called pure comedies Bleak. Direct title evidence can override these coarse
# compatibility sets, preserving explicitly hopeful dramas and dark comedies.
POSITIVE_MOOD_NAMES: frozenset[str] = frozenset(
    {
        "Feel-Good",
        "Heartwarming",
        "Lighthearted",
        "Cozy",
        "Hopeful",
        "Rousing",
        "Whimsical",
        "Tender",
    }
)
POSITIVE_MOOD_GENRE_LABELS: frozenset[str] = frozenset(
    normalize(value)
    for value in (
        "Comedy",
        "Documentary",
        "Family",
        "Kids",
        "Music",
        "Reality",
        "Romance",
        "Talk",
    )
)
HARSH_MOOD_NAMES: frozenset[str] = frozenset(
    {"Dark", "Bleak", "Brutal", "Chilling", "Unsettling"}
)
HARSH_MOOD_GENRE_LABELS: frozenset[str] = frozenset(
    normalize(value)
    for value in (
        "Crime",
        "Horror",
        "Mystery",
        "Thriller",
        "War",
        "War & Politics",
    )
)

# Genre is only a compatibility rail for these tags; it is not positive
# evidence. This blocks labels such as Barbie -> Suspenseful and 9-1-1: Lone
# Star -> Witty before a model call while preserving them for the genres where
# they can plausibly describe the work as a whole.
HIGH_TENSION_MOOD_NAMES: frozenset[str] = frozenset({"Suspenseful", "Tense", "Intense"})
HIGH_TENSION_MOOD_GENRE_LABELS: frozenset[str] = frozenset(
    normalize(value)
    for value in (
        "Action",
        "Action & Adventure",
        "Crime",
        "Horror",
        "Mystery",
        "Thriller",
        "War",
        "War & Politics",
    )
)
WITTY_GENRE_LABELS: frozenset[str] = frozenset(
    normalize(value) for value in ("Comedy", "Talk")
)
CHILD_OR_FAMILY_GENRE_LABELS: frozenset[str] = frozenset(
    normalize(value) for value in ("Kids", "Family")
)

# An exact provider keyword is normally useful mood evidence. These two labels
# proved too noisy when that keyword contradicts Crime/War/Horror metadata, so
# the title or synopsis must carry the tone as well.
NARRATIVE_ONLY_HARSH_POSITIVE_MOOD_NAMES: frozenset[str] = frozenset(
    {"Lighthearted", "Rousing"}
)

# These archetypes routinely describe a supporting character rather than the
# work's protagonist. Centrality must be visible in the synopsis before the
# model may apply the title-level label. More explicit concepts such as
# "female protagonist" and "rivalry" remain useful trusted keyword evidence.
CHARACTER_SYNOPSIS_EVIDENCE_PHRASES: dict[str, tuple[str, ...]] = {
    "Anti-Hero": (
        "anti hero",
        "antihero",
        "mercenary",
        "vigilante",
        "bounty hunter",
        "bounty hunters",
        "morally flexible",
        "criminal protagonist",
        "disillusioned castoffs",
        "punish criminals",
        "punish the criminals",
        "revenge",
    ),
    "Femme Fatale": ("femme fatale",),
    "Morally Ambiguous": (
        "morally ambiguous",
        "morally grey",
        "moral ambiguity",
        "torn between right and wrong",
    ),
}

# Some concepts are too easy to retrieve from one ambiguous token. These
# phrases are deliberately precise and are checked against synopsis/TMDB facts,
# not reviews. The model still judges whether the offered concept is central.
BRUTAL_EVIDENCE_PHRASES: tuple[str, ...] = (
    "brutal",
    "brutality",
    "graphic violence",
    "extreme violence",
    "gore",
    "gruesome",
    "torture",
    "war crime",
    "war crimes",
)

# These phrases contain a mood word but describe a person, behaviour or
# economic condition rather than the tone of the work. They came directly from
# the job 228 adversarial export. A separate exact TMDB keyword can still
# establish the mood; the ambiguous synopsis phrase alone cannot.
MOOD_FALSE_FRIEND_PHRASES: dict[str, tuple[str, ...]] = {
    "Brutal": ("brutal honesty",),
    "Chilling": ("chilling recession",),
    "Hopeful": ("the hopeful",),
}
STRICT_THEME_EVIDENCE_PHRASES: dict[str, tuple[str, ...]] = {
    "AI Ethics": (
        "ai ethics",
        "artificial intelligence ethics",
        "machine ethics",
        "robot ethics",
        "ai rights",
        "robot rights",
    ),
    "Mortality": (
        "mortality",
        "dying",
        "afterlife",
        "life after death",
        "immortal",
        "immortality",
        "funeral",
        "terminal illness",
        "confronts death",
        "facing death",
    ),
    "Religion and Cults": (
        "religion",
        "religious faith",
        "religious cult",
        "cult leader",
        "cult member",
        "religious institution",
        "church power",
        "christianity",
        "christian faith",
        "jesus christ",
        "biblical",
    ),
}

# Family as a broad TMDB audience genre is not evidence for Family Drama. The
# narrative itself must identify family/relative relationships as story matter.
FAMILY_DRAMA_EVIDENCE_PHRASES: tuple[str, ...] = (
    "family",
    "families",
    "relative",
    "relatives",
    "inheritance",
    "mother",
    "father",
    "parent",
    "parents",
    "brother",
    "sister",
    "sibling",
    "siblings",
    "son",
    "daughter",
)

# A TV keyword can describe one episode while the stored assignment describes
# the complete series. Job 225 consequently made time travel a defining label
# for Gravity Falls, Ben 10 and The Twilight Zone. These high-impact concepts
# now require the series synopsis itself to establish the recurring premise.
TV_WHOLE_WORK_EVIDENCE_PHRASES: dict[str, tuple[str, ...]] = {
    "Time Travel": (
        "time travel",
        "time traveler",
        "time travellers",
        "time machine",
        "through time",
        "back in time",
        "different time periods",
        "spans three generations",
        "spanning three generations",
    ),
    "Time Travel Sci-Fi": (
        "time travel",
        "time traveler",
        "time travellers",
        "time machine",
        "through time",
        "back in time",
        "different time periods",
        "spans three generations",
        "spanning three generations",
    ),
}

TV_HIGH_SCHOOL_EVIDENCE_PHRASES: tuple[str, ...] = (
    "high school",
    "secondary school",
    "school student",
    "school life",
    "school academy",
    "navigate school",
    "at school",
)

TV_PLOT_NARRATIVE_EVIDENCE_PHRASES: dict[str, tuple[str, ...]] = {
    "Detective": (
        "detective",
        "investigator",
        "investigates",
        "investigation",
        "solve crimes",
        "solve a case",
        "crime solving",
    ),
    "Missing Person": ("missing person", "missing child", "vanished"),
    "Survival": ("survival", "survivor", "survive", "staying alive"),
    "Hostage Crisis": (
        "hostage crisis",
        "hostages",
        "hijacker",
        "hijackers",
        "airplane hijacking",
    ),
}

# These labels describe series structure and therefore cannot be established by
# an episode-level keyword. The synopsis must say that the work itself uses the
# format. This blocks The Residence -> Anthology while preserving true
# anthology series whose overview explicitly identifies the structure.
TV_FORMAT_NARRATIVE_NAMES: frozenset[str] = frozenset(
    {"Anthology", "Episodic", "Serialized"}
)

# The model is deliberately limited to genuinely interpretive judgements.
# Everything else is written by ``deterministic_semantic_tags`` when explicit
# metadata supports it. Subjective pacing remains a model judgement even though
# it shares a facet with factual formats such as Anthology and Serialized.
MODEL_INTERPRETIVE_FACETS: frozenset[str] = frozenset({"mood", "theme", "character"})
MODEL_INTERPRETIVE_PACING_NAMES: frozenset[str] = frozenset(
    {
        "Slow-Burn",
        "Fast-Paced",
        "Breakneck",
        "Bingeable",
        "Meditative Pace",
        "Twisty",
        "Slow Start",
    }
)


def is_model_interpretive(tag: TagDef) -> bool:
    """Whether a tag may be selected by the paid model in taxonomy v5."""

    return tag.facet in MODEL_INTERPRETIVE_FACETS or (
        tag.name in MODEL_INTERPRETIVE_PACING_NAMES
    )


# Some recurring TV themes are described naturally without repeating the
# taxonomy label. Keep those synopsis-level paraphrases while still rejecting
# MDBList-only episode subjects.
TV_THEME_NARRATIVE_EVIDENCE_PHRASES: dict[str, tuple[str, ...]] = {
    "Friendship": (
        "drawn together",
        "group of friends",
        "close friends",
        "best friends",
        "unlikely friends",
    ),
}

# A location mentioned in a TV synopsis can be backstory rather than the
# recurring setting. Arrow's yacht being "lost at sea" and a character being
# "fresh out of prison" in Mayans M.C. do not make either series an At Sea or
# Prison shelf. Exact TMDB keywords remain authoritative; otherwise require a
# phrase that describes sustained life in that setting.
TV_RECURRING_PLACE_EVIDENCE_PHRASES: dict[str, tuple[str, ...]] = {
    "At Sea": (
        "aboard a ship",
        "aboard the ship",
        "aboard a submarine",
        "on a naval vessel",
        "ship s crew",
        "ocean voyage",
    ),
    "Prison": (
        "inside a prison",
        "prison inmate",
        "prison inmates",
        "prison guard",
        "prison guards",
        "correctional facility",
        "behind bars",
        "incarcerated",
    ),
}

# MDBList keywords add useful coverage but occasionally contain data from a
# different title. Exact named cities/regions are sufficiently distinctive to
# accept from that source; generic settings such as Island or The Desert are
# not, because one contaminated word creates a visibly absurd shelf match.
MDB_NAMED_PLACE_NAMES: frozenset[str] = frozenset(
    {
        "Amsterdam",
        "Berlin",
        "Chicago",
        "Hong Kong",
        "Las Vegas",
        "London",
        "Los Angeles",
        "Mexico City",
        "Miami",
        "New York City",
        "Paris",
        "Rome",
        "Seoul",
        "Shanghai",
        "The Scottish Highlands",
        "The Vatican",
        "Tokyo",
        "Washington D.C.",
    }
)

_STOPWORDS = frozenset(
    "a an and are as at be by for from in into is it its of on or that the their"
    " them there these they this to was were with without you your".split()
)


_SEMANTIC_ROMAN_NUMERALS = frozenset({"i", "ii", "iii", "iv", "v", "vi"})


def _tokens(text: str) -> set[str]:
    return {
        word
        for word in normalize(text).split()
        if (len(word) > 2 or word in _SEMANTIC_ROMAN_NUMERALS)
        and word not in _STOPWORDS
    }


def _build_keyword_index() -> dict[str, set[str]]:
    """Map a normalized token to the canonical tag names it suggests.

    Built once at import from each tag's name, aliases and gloss. This is what
    makes ``shortlist()`` deterministic and free -- no model call, no DB.
    """
    index: dict[str, set[str]] = {}
    for tag in TAXONOMY:
        if tag.derived:
            continue
        if tag.facet not in RETRIEVED_FACETS:
            continue
        sources = [tag.name, *tag.aliases, tag.gloss]
        for source in sources:
            for token in _tokens(source):
                index.setdefault(token, set()).add(tag.name)
        # The whole normalized name is also a key, so multi-word tags match
        # a TMDB keyword like "time travel" exactly.
        index.setdefault(normalize(tag.name), set()).add(tag.name)
        for alias in tag.aliases:
            index.setdefault(normalize(alias), set()).add(tag.name)
    return index


KEYWORD_INDEX: dict[str, set[str]] = _build_keyword_index()


# ---------------------------------------------------------------------------
# Derived tags -- computed, never offered to the model
# ---------------------------------------------------------------------------
_LANG_ORIGIN: dict[str, str] = {
    "ko": "Korean",
    "ja": "Japanese",
    "fr": "French",
    "it": "Italian",
    "es": "Spanish",
    "zh": "Chinese",
    "cn": "Chinese",
    "hi": "Indian",
    "ta": "Indian",
    "te": "Indian",
    "ml": "Indian",
    "de": "German",
    "ru": "Russian",
    "pl": "Polish",
    "tr": "Turkish",
    "he": "Israeli",
    "fa": "Iranian",
    "th": "Thai",
    "tl": "Filipino",
    "pt": "Brazilian",
}

_COUNTRY_ORIGIN: dict[str, str] = {
    "US": "American",
    "GB": "British",
    "UK": "British",
    "IE": "Irish",
    "KR": "Korean",
    "JP": "Japanese",
    "FR": "French",
    "IT": "Italian",
    "ES": "Spanish",
    "CN": "Chinese",
    "HK": "Hong Kong",
    "TW": "Taiwanese",
    "IN": "Indian",
    "DE": "German",
    "AU": "Australian",
    "CA": "Canadian",
    "MX": "Mexican",
    "BR": "Brazilian",
    "AR": "Argentine",
    "RU": "Russian",
    "PL": "Polish",
    "TR": "Turkish",
    "IL": "Israeli",
    "IR": "Iranian",
    "NG": "Nigerian",
    "TH": "Thai",
    "PH": "Filipino",
}

_NORDIC = frozenset({"SE", "NO", "DK", "FI", "IS"})
_LATIN_AMERICAN = frozenset(
    {"MX", "BR", "AR", "CL", "CO", "PE", "UY", "VE", "CU", "BO", "EC", "PY", "CR", "GT"}
)
_EUROPEAN = frozenset(
    {
        "GB",
        "IE",
        "FR",
        "IT",
        "ES",
        "DE",
        "PT",
        "NL",
        "BE",
        "AT",
        "CH",
        "PL",
        "CZ",
        "SK",
        "HU",
        "RO",
        "BG",
        "GR",
        "SE",
        "NO",
        "DK",
        "FI",
        "IS",
        "HR",
        "RS",
        "SI",
        "EE",
        "LV",
        "LT",
        "UA",
        "LU",
        "MT",
        "CY",
    }
)


def _decade_tag(release_date: str | None) -> str | None:
    """Map a TMDB YYYY-MM-DD string to its release_decade tag name."""
    if not release_date or len(release_date) < 4 or not release_date[:4].isdigit():
        return None
    year = int(release_date[:4])
    decade = (year // 10) * 10
    name = f"{decade}s"
    return name if name in BY_NAME else None


def derive_tags(features: dict[str, Any]) -> dict[str, float]:
    """Compute the tags that are read from metadata rather than inferred.

    These are never placed in the prompt. Deriving them removes the largest
    single hallucination class (the model guessing an era or a country from
    tone) and keeps every era/origin assignment correct by construction.

    Args:
        features: A metadata dict. Recognised keys are ``media_type``,
            ``release_date``, ``original_language``, ``countries``
            (ISO-3166 alpha-2), ``runtime`` (minutes), ``number_of_seasons``
            and ``number_of_episodes``. Missing keys are simply skipped.

    Returns:
        ``{tag_name: confidence}``. Derived tags carry 1.0 -- they are facts,
        not judgements.
    """
    out: dict[str, float] = {}
    media_type = features.get("media_type") or "movie"

    # TMDB genres are curated metadata, not model judgement. Storing them as
    # derived facts guarantees that a truncated response cannot turn Breaking
    # Bad into a title with neither Crime nor Drama.
    genres: list[Any] = list(features.get("genres") or [])
    for source_key in ("tag_facts", "mdb_facts"):
        source = features.get(source_key)
        if isinstance(source, dict):
            genres.extend(source.get("genres") or [])
    for genre in genres:
        for name in _tmdb_genre_names(features, genre, derived=True):
            tag = BY_NAME.get(name)
            if tag is not None and media_type in tag.media:
                out[name] = 1.0

    decade = _decade_tag(features.get("release_date"))
    if decade:
        out[decade] = 1.0

    countries = [str(c).upper() for c in (features.get("countries") or []) if c]
    language = (features.get("original_language") or "").lower()

    # Production countries are ordered. Storing every co-producer plus its
    # regional parent gave some titles American + British + European and
    # triple-weighted one facet. Use the first supported specific origin; only
    # fall back to a regional umbrella when no specific tag exists.
    primary_country = countries[0] if countries else ""
    primary_origin = _COUNTRY_ORIGIN.get(primary_country)
    if primary_origin:
        out[primary_origin] = 1.0
    elif primary_country in _NORDIC:
        out["Scandinavian"] = 1.0
    elif primary_country in _LATIN_AMERICAN:
        out["Latin American"] = 1.0
    elif primary_country in _EUROPEAN:
        out["European"] = 1.0

    if not any(BY_NAME[name].facet == "origin" for name in out):
        name = _LANG_ORIGIN.get(language)
        if name:
            out[name] = 1.0

    runtime = features.get("runtime")
    if media_type == "movie" and isinstance(runtime, (int, float)) and runtime > 0:
        if runtime < 90:
            out["Under 90 Minutes"] = 1.0
        elif runtime >= 150:
            out["Epic Runtime"] = 1.0
        else:
            out["Feature Length"] = 1.0

    if media_type == "tv":
        seasons = features.get("number_of_seasons")
        episodes = features.get("number_of_episodes")
        if isinstance(seasons, int) and seasons > 0:
            if seasons == 1:
                out["Single Season"] = 1.0
                if isinstance(episodes, int) and 0 < episodes <= 10:
                    out["Limited Series"] = 1.0
            elif seasons >= 4:
                out["Multi-Season Saga"] = 1.0

    # Explicit semantic facts use the same evidence guards as shortlisting but
    # are written locally rather than delegated to a probabilistic model.
    out.update(deterministic_semantic_tags(features))

    # TMDB's Family genre describes subject matter as well as audience. An
    # explicit adult-animation fact is more specific and must win for audience
    # targeting, while the broad Family subgenre remains useful for discovery.
    if "Adult Animation" in out:
        for incompatible in ("Kids", "Preschool", "All Ages", "Family Movie Night"):
            out.pop(incompatible, None)

    # Never emit a tag the media type disallows.
    return {
        name: score
        for name, score in out.items()
        if name in BY_NAME and media_type in BY_NAME[name].media
    }


# ---------------------------------------------------------------------------
# Shortlist and batching
# ---------------------------------------------------------------------------
def _feature_text(features: dict[str, Any], *, include_mdb: bool = True) -> str:
    """Flatten authoritative fields worth matching for candidate retrieval.

    Reviews are intentionally excluded. Job 226 demonstrated that a single
    critic phrase such as "surprisingly warm" or "justice is served" can turn
    into a confident whole-work tag. Cast, directors, studios and networks have
    similar proper-noun collision problems and never drive retrieval.
    """
    parts: list[str] = [
        str(features.get("title") or ""),
        str(features.get("overview") or ""),
    ]
    # Production feature rows keep enrichment under these nested objects. MDB
    # keywords improve recall for general semantic concepts. The caller can
    # still exclude MDBList entirely for places and parent-bypassing specialist
    # subgenres because those are especially vulnerable to title contamination.
    sources = [features]
    source_keys = ("tag_facts", "mdb_facts") if include_mdb else ("tag_facts",)
    for key in source_keys:
        value = features.get(key)
        if isinstance(value, dict):
            sources.append(value)
    for source in sources:
        for key in ("keywords", "genres"):
            value = source.get(key)
            if isinstance(value, (list, tuple)):
                parts.extend(str(v) for v in value)
            elif value:
                parts.append(str(value))
        collection = source.get("collection")
        if collection:
            parts.append(str(collection))
    return " ".join(parts)


def shortlist(features: dict[str, Any]) -> list[TagDef]:
    """Build the candidate tag menu offered for one item.

    Broad TMDB genres are pinned, a compact judgement core is always available,
    and every specific concept must be retrieved from title evidence. There is
    deliberately no taxonomy-order padding: offering ``Remake``, ``Feudal
    Japan`` or ``Alien Contact`` merely because a facet came up short was the
    dominant source of confident nonsense in the production pilot.
    """
    media_type = features.get("media_type") or "movie"

    def usable(tag: TagDef) -> bool:
        return (
            not tag.derived
            and media_type in tag.media
            and tag.name not in PROMPT_SUPPRESSED_NAMES
        )

    chosen: dict[str, TagDef] = {}
    pinned: set[str] = set()
    genre_support: set[str] = set()
    genres: list[Any] = list(features.get("genres") or [])
    for source_key in ("tag_facts", "mdb_facts"):
        source = features.get(source_key)
        if isinstance(source, dict):
            genres.extend(source.get("genres") or [])
    genre_labels = {normalize(genre) for genre in genres if genre}
    for genre in genres:
        factual_names = set(_tmdb_genre_names(features, genre, derived=True))
        genre_support.update(factual_names)
        for name in _tmdb_genre_names(features, genre, derived=False):
            # Broad TMDB genres already land as deterministic rows. Offering
            # them to the model wastes its small semantic budget, and when the
            # derived write overwrites the same name it can make a healthy
            # response look as if too few model judgements survived.
            if name in factual_names:
                continue
            tag = BY_NAME.get(name)
            if tag is not None and usable(tag):
                chosen[name] = tag
                pinned.add(name)
                genre_support.add(name)

    core_names = set(CORE_OFFERED_NAMES)
    core_names.difference_update(EVIDENCE_ONLY_MOOD_NAMES)
    if media_type == "tv":
        core_names.update(TV_CORE_OFFERED_NAMES)
    for name in core_names:
        tag = BY_NAME.get(name)
        if tag is not None and usable(tag):
            chosen[name] = tag

    text = normalize(_feature_text(features))
    factual_text = text
    trusted_text = normalize(_feature_text(features, include_mdb=False))
    text_tokens = _tokens(text)
    evidence_values: list[Any] = []
    trusted_evidence_values: list[Any] = []
    for source in (
        features,
        features.get("tag_facts") or {},
        features.get("mdb_facts") or {},
    ):
        if not isinstance(source, dict):
            continue
        evidence_values.extend(source.get("keywords") or [])
        evidence_values.extend(source.get("genres") or [])
        if source is not features.get("mdb_facts"):
            trusted_evidence_values.extend(source.get("keywords") or [])
            trusted_evidence_values.extend(source.get("genres") or [])
    evidence_phrases = {normalize(value) for value in evidence_values if value}
    trusted_evidence_phrases = {
        normalize(value) for value in trusted_evidence_values if value
    }
    padded_text = f" {text} "
    factual_padded_text = f" {factual_text} "
    trusted_padded_text = f" {trusted_text} "
    trusted_narrative_values: list[Any] = [
        features.get("title"),
        features.get("overview"),
    ]
    for source in (features, features.get("tag_facts") or {}):
        if not isinstance(source, dict):
            continue
        value = source.get("keywords")
        if isinstance(value, (list, tuple)):
            trusted_narrative_values.extend(value)
        elif value:
            trusted_narrative_values.append(value)
    trusted_narrative_text = normalize(
        " ".join(str(value) for value in trusted_narrative_values if value)
    )
    trusted_narrative_padded_text = f" {trusted_narrative_text} "
    mdb_source = features.get("mdb_facts") or {}
    mdb_values: list[Any] = []
    if isinstance(mdb_source, dict):
        mdb_values.extend(mdb_source.get("keywords") or [])
        mdb_values.extend(mdb_source.get("genres") or [])
    mdb_phrases = {normalize(value) for value in mdb_values if value}
    title_overview_text = normalize(
        " ".join(
            str(value)
            for value in (features.get("title"), features.get("overview"))
            if value
        )
    )
    title_overview_padded_text = f" {title_overview_text} "
    overview_padded_text = f" {normalize(features.get('overview') or '')} "
    raw_countries: list[Any] = list(features.get("countries") or [])
    tag_facts = features.get("tag_facts") or {}
    if isinstance(tag_facts, dict):
        raw_countries.extend(tag_facts.get("countries") or [])
    if isinstance(mdb_source, dict) and mdb_source.get("country"):
        raw_countries.append(mdb_source["country"])
    country_codes = {normalize(value) for value in raw_countries if value}
    original_language = normalize(features.get("original_language") or "")

    def has_phrase(source: str, phrases: Iterable[str]) -> bool:
        return any(f" {normalize(phrase)} " in source for phrase in phrases)

    hits: list[tuple[int, TagDef]] = []
    for tag in TAXONOMY:
        if not usable(tag) or tag.facet not in RETRIEVED_FACETS:
            continue
        if tag.name in TMDB_PINNED_TAG_NAMES and tag.name not in pinned:
            continue
        if tag.name == "Adult Animation" and not any(
            phrase in text
            for phrase in (
                "adult animation",
                "adult cartoon",
                "animation for adults",
                "mature animation",
            )
        ):
            continue
        variants = {normalize(tag.name), *(normalize(alias) for alias in tag.aliases)}
        variants.discard("")
        factual_direct_evidence = bool(variants & evidence_phrases) or any(
            f" {variant} " in factual_padded_text for variant in variants
        )
        strict_direct_evidence = bool(variants & evidence_phrases) or any(
            f" {variant} " in title_overview_padded_text for variant in variants
        )
        narrative_direct_evidence = any(
            f" {variant} " in title_overview_padded_text for variant in variants
        )

        # High-risk TMDB character keywords can describe a supporting role or
        # one arc. The model may see these archetypes only when the synopsis
        # establishes them as title-level evidence.
        character_narrative_phrases = CHARACTER_SYNOPSIS_EVIDENCE_PHRASES.get(
            tag.name, ()
        )
        if character_narrative_phrases and not has_phrase(
            title_overview_padded_text, character_narrative_phrases
        ):
            continue

        false_friend_phrases = MOOD_FALSE_FRIEND_PHRASES.get(tag.name, ())
        if (
            false_friend_phrases
            and has_phrase(title_overview_padded_text, false_friend_phrases)
            and not bool(variants & trusted_evidence_phrases)
        ):
            continue
        canonical_variant = normalize(tag.name)
        narrative_place_variants = {
            canonical_variant,
            *(variant for variant in variants if len(normalize(variant).split()) >= 2),
        }
        trusted_keyword_place_evidence = bool(variants & trusted_evidence_phrases)
        narrative_place_evidence = any(
            f" {variant} " in title_overview_padded_text
            for variant in narrative_place_variants
        )
        named_mdb_place_evidence = tag.name in MDB_NAMED_PLACE_NAMES and bool(
            variants & mdb_phrases
        )
        place_evidence = (
            trusted_keyword_place_evidence
            or narrative_place_evidence
            or named_mdb_place_evidence
        )

        if media_type == "tv" and tag.name in TV_RECURRING_PLACE_EVIDENCE_PHRASES:
            recurring_place_evidence = bool(
                variants & trusted_evidence_phrases
            ) or has_phrase(
                title_overview_padded_text,
                TV_RECURRING_PLACE_EVIDENCE_PHRASES[tag.name],
            )
            if not recurring_place_evidence:
                continue
            place_evidence = True

        if tag.facet == "place" and not place_evidence:
            continue

        if tag.name == "Small-Town America":
            explicitly_american = has_phrase(
                title_overview_padded_text,
                (
                    "small town america",
                    "small town usa",
                    "american small town",
                    "united states",
                ),
            )
            if country_codes and not (
                {"us", "usa", "united states", "united states of america"}
                & country_codes
            ):
                continue
            if not country_codes and not explicitly_american:
                continue

        if (
            media_type == "tv"
            and tag.name == "High School"
            and not has_phrase(
                title_overview_padded_text, TV_HIGH_SCHOOL_EVIDENCE_PHRASES
            )
        ):
            continue

        whole_work_evidence = (
            media_type == "tv"
            and tag.name in TV_WHOLE_WORK_EVIDENCE_PHRASES
            and has_phrase(
                title_overview_padded_text,
                TV_WHOLE_WORK_EVIDENCE_PHRASES[tag.name],
            )
        )
        if (
            media_type == "tv"
            and tag.name in TV_WHOLE_WORK_EVIDENCE_PHRASES
            and not whole_work_evidence
        ):
            continue

        if tag.name == "Anime":
            explicit_anime = (
                "anime" in evidence_phrases
                or "japanese animation" in evidence_phrases
                or has_phrase(
                    title_overview_padded_text, ("anime", "japanese animation")
                )
            )
            japanese_origin = original_language == "ja" or bool(
                {"jp", "japan"} & country_codes
            )
            if not (explicit_anime and japanese_origin):
                continue

        if tag.name == "Cinematic Universe":
            cinematic_universe_evidence = has_phrase(
                factual_padded_text,
                (
                    "cinematic universe",
                    "marvel cinematic universe",
                    "dc extended universe",
                    "dc universe",
                ),
            )
            if not cinematic_universe_evidence:
                continue
            # A keyword commonly includes a suffix such as "(MCU)", so it is
            # not always an exact member of ``evidence_phrases``. The explicit
            # named phrase above is nevertheless strict factual evidence.
            strict_direct_evidence = True
            factual_direct_evidence = True

        if (
            media_type == "tv"
            and tag.facet == "plot_device"
            and not whole_work_evidence
        ):
            plot_phrases = {
                normalize(tag.name),
                *(
                    variant
                    for variant in variants
                    if len(normalize(variant).split()) >= 2
                ),
                *(
                    normalize(phrase)
                    for phrase in TV_PLOT_NARRATIVE_EVIDENCE_PHRASES.get(tag.name, ())
                ),
            }
            if not any(
                f" {phrase} " in title_overview_padded_text for phrase in plot_phrases
            ):
                continue

        if (
            media_type == "tv"
            and tag.name in TV_FORMAT_NARRATIVE_NAMES
            and not any(
                f" {variant} " in title_overview_padded_text
                for variant in {
                    normalize(tag.name),
                    *(
                        variant
                        for variant in variants
                        if len(normalize(variant).split()) >= 2
                    ),
                }
            )
        ):
            continue

        if (
            media_type == "tv"
            and tag.name == "Kaiju"
            and not has_phrase(title_overview_padded_text, ("kaiju", "giant monster"))
        ):
            continue

        if (
            tag.name == "Educational"
            and not (
                genre_labels
                & {normalize("Documentary"), normalize("Kids"), normalize("Family")}
            )
            and not has_phrase(
                title_overview_padded_text,
                ("educational", "instructional", "teaches", "learning series"),
            )
        ):
            continue

        if tag.name in {"CG Animation", "Cel Animation", "Stop-Motion", "Rotoscope"}:
            explicitly_animated_work = normalize(
                "Animation"
            ) in genre_labels or has_phrase(
                title_overview_padded_text,
                ("animated film", "animated series", "animation", "stop motion"),
            )
            if not explicitly_animated_work:
                continue
        if (
            tag.facet in STRICT_EVIDENCE_FACETS
            and tag.facet != "place"
            and not strict_direct_evidence
        ):
            continue

        if media_type == "tv" and tag.facet == "maturity":
            trusted_maturity_evidence = any(
                f" {variant} " in title_overview_padded_text for variant in variants
            )
            if not trusted_maturity_evidence:
                continue

        if (
            media_type == "tv"
            and tag.facet == "seasonal"
            and not any(
                f" {variant} " in title_overview_padded_text for variant in variants
            )
        ):
            continue

        if tag.name in UNSCRIPTED_FORMAT_TAG_NAMES and not (
            genre_labels & UNSCRIPTED_GENRE_LABELS
        ):
            strong_format_variants = {
                variant for variant in variants if len(_tokens(variant)) >= 2
            }
            explicit_format_evidence = bool(
                strong_format_variants & evidence_phrases
            ) or any(
                f" {variant} " in overview_padded_text
                for variant in strong_format_variants
            )
            if not explicit_format_evidence:
                continue

        strict_theme_evidence = (
            tag.name in STRICT_THEME_EVIDENCE_PHRASES
            and has_phrase(factual_padded_text, STRICT_THEME_EVIDENCE_PHRASES[tag.name])
        )
        if tag.name in STRICT_THEME_EVIDENCE_PHRASES and not strict_theme_evidence:
            chosen.pop(tag.name, None)
            continue

        # MDBList aggregates can contain episode-level subjects that do not
        # define an entire series. Degrassi, for example, carried one
        # "environmentalism" keyword across fourteen seasons. TV themes now
        # need support from the synopsis or TMDB-owned facts.
        trusted_theme_evidence = (
            bool(variants & trusted_evidence_phrases)
            or any(f" {variant} " in title_overview_padded_text for variant in variants)
            or has_phrase(
                title_overview_padded_text,
                TV_THEME_NARRATIVE_EVIDENCE_PHRASES.get(tag.name, ()),
            )
        )
        if (
            media_type == "tv"
            and tag.facet == "theme"
            and not (trusted_theme_evidence or strict_theme_evidence)
        ):
            chosen.pop(tag.name, None)
            continue

        if tag.name == "Family Drama" and not has_phrase(
            trusted_narrative_padded_text, FAMILY_DRAMA_EVIDENCE_PHRASES
        ):
            chosen.pop(tag.name, None)
            continue

        if tag.name == "Brutal" and not has_phrase(
            factual_padded_text, BRUTAL_EVIDENCE_PHRASES
        ):
            chosen.pop(tag.name, None)
            continue

        if (
            tag.name in HIGH_TENSION_MOOD_NAMES
            and not (genre_labels & HIGH_TENSION_MOOD_GENRE_LABELS)
            and not factual_direct_evidence
        ):
            chosen.pop(tag.name, None)
            continue

        if (
            tag.name == "Witty"
            and not (genre_labels & WITTY_GENRE_LABELS)
            and not factual_direct_evidence
        ):
            chosen.pop(tag.name, None)
            continue

        if (
            tag.name == "Cerebral"
            and (genre_labels & CHILD_OR_FAMILY_GENRE_LABELS)
            and not factual_direct_evidence
        ):
            chosen.pop(tag.name, None)
            continue

        if (
            tag.name in POSITIVE_MOOD_NAMES
            and not (genre_labels & POSITIVE_MOOD_GENRE_LABELS)
            and not factual_direct_evidence
        ):
            chosen.pop(tag.name, None)
            continue
        if (
            tag.name in POSITIVE_MOOD_NAMES
            and (genre_labels & HARSH_MOOD_GENRE_LABELS)
            and not (genre_labels & CHILD_OR_FAMILY_GENRE_LABELS)
            and not factual_direct_evidence
        ):
            # A comedy secondary genre must not turn a crime/horror series
            # into Feel-Good or Hopeful. Explicit factual tone evidence can
            # still preserve genuinely warm genre hybrids.
            chosen.pop(tag.name, None)
            continue
        if (
            tag.name in NARRATIVE_ONLY_HARSH_POSITIVE_MOOD_NAMES
            and (genre_labels & HARSH_MOOD_GENRE_LABELS)
            and not (genre_labels & CHILD_OR_FAMILY_GENRE_LABELS)
            and not narrative_direct_evidence
        ):
            # For the two noisiest labels, a provider keyword alone cannot
            # override a directly contradictory genre profile.
            chosen.pop(tag.name, None)
            continue
        if (
            tag.name in HARSH_MOOD_NAMES
            and not (genre_labels & HARSH_MOOD_GENRE_LABELS)
            and not factual_direct_evidence
        ):
            chosen.pop(tag.name, None)
            continue
        if (
            tag.name in HARSH_MOOD_NAMES
            and (genre_labels & CHILD_OR_FAMILY_GENRE_LABELS)
            and not factual_direct_evidence
        ):
            # Mystery is common in family animation and is not, by itself,
            # evidence that the whole work is Dark, Bleak or Unsettling.
            chosen.pop(tag.name, None)
            continue

        # A specialist subgenre can contradict the authoritative broad genre
        # even while sharing a plausible word with the synopsis. Require its
        # parent genre, or an exact specialist name/alias in TMDB evidence.
        # This removes Science Documentary from Interstellar and Creature
        # Feature from Harry Potter without losing exact signals such as
        # "post-apocalyptic", "vampire" or "dark comedy" on records whose
        # coarse TMDB genre omitted their semantic parent.
        if tag.facet == "subgenre" and tag.parent:
            canonical = normalize(tag.name)
            strong_aliases = {
                normalize(alias)
                for alias in tag.aliases
                if normalize(alias) not in {"spy", "espionage"}
            }
            exact_specialist_evidence = (
                canonical in trusted_evidence_phrases
                or f" {canonical} " in trusted_padded_text
                or bool(strong_aliases & trusted_evidence_phrases)
                or any(f" {alias} " in trusted_padded_text for alias in strong_aliases)
            )
            if tag.parent not in genre_support and not exact_specialist_evidence:
                continue

        # "Long Island" and "Rhode Island" are named regions, not evidence
        # that the work has an isolated-island setting. An exact standalone
        # keyword still wins when the title genuinely uses both senses.
        if (
            tag.name == "Island"
            and not (variants & trusted_evidence_phrases)
            and any(
                phrase in trusted_padded_text
                for phrase in (" long island ", " rhode island ")
            )
        ):
            continue
        score = 0
        if whole_work_evidence:
            score += 100
        if strict_theme_evidence:
            score += 30
        if tag.name == "Family Drama" and has_phrase(
            trusted_narrative_padded_text, FAMILY_DRAMA_EVIDENCE_PHRASES
        ):
            score += 30
        if variants & evidence_phrases:
            score += 100
        for variant in variants:
            if f" {variant} " in padded_text:
                score += 30
            variant_tokens = _tokens(variant)
            overlap = len(variant_tokens & text_tokens)
            if (
                len(normalize(variant).split()) == 1
                and variant_tokens
                and overlap == len(variant_tokens)
            ):
                score += 12 + overlap

        # Glosses help retrieve a specific concept from descriptive prose, but
        # one generic word is never enough. For example, "story" must not make
        # every plot-device tag legal.
        gloss_overlap = len(_tokens(tag.gloss) & text_tokens)
        if gloss_overlap >= 2:
            score += gloss_overlap * 2
        if score > 0 and tag.parent in genre_support:
            score += 2
        if score >= 12 and tag.name not in chosen:
            hits.append((score, tag))

    facet_counts: dict[str, int] = {facet: 0 for facet in RETRIEVED_FACETS}
    for _score, tag in sorted(
        hits,
        key=lambda value: (-value[0], value[1].sort_hint, value[1].name),
    ):
        if len(chosen) >= SHORTLIST_MAX:
            break
        limit = FACET_RETRIEVAL_LIMITS.get(tag.facet, RETRIEVED_PER_FACET)
        if facet_counts[tag.facet] >= limit:
            continue
        chosen[tag.name] = tag
        facet_counts[tag.facet] += 1

    ordered = sorted(
        chosen.values(),
        key=lambda t: (FACETS.index(t.facet), t.sort_hint, t.name),
    )
    if len(ordered) > SHORTLIST_MAX:
        raise RuntimeError(
            f"shortlist budget invariant broken: {len(ordered)} > {SHORTLIST_MAX}"
        )
    return ordered


def model_shortlist(features: dict[str, Any]) -> list[TagDef]:
    """Return only evidence-backed tags that require model judgement.

    MDBList aggregates are deliberately removed for this pass. They remain
    useful for deterministic factual labels such as Anime or Based on a Book,
    but job 227 showed that their episode-level concepts can turn into whole-
    series character and theme judgements (Rebelde -> Anti-Hero).
    """

    trusted = dict(features)
    trusted["mdb_facts"] = {}
    return [tag for tag in shortlist(trusted) if is_model_interpretive(tag)]


def _contains_variant(text: str, variants: Iterable[str]) -> bool:
    padded = f" {normalize(text)} "
    return any(f" {normalize(variant)} " in padded for variant in variants)


def deterministic_semantic_tags(features: dict[str, Any]) -> dict[str, float]:
    """Extract explicit, non-interpretive taxonomy facts without an API call.

    Candidate generation still supplies the hard safety rails for media type,
    parent genre, TV whole-work evidence and ambiguous places. This second
    boundary requires literal support in a synopsis or keyword, preventing a
    gloss overlap from becoming a deterministic fact.
    """

    facts = features.get("tag_facts") or {}
    mdb = features.get("mdb_facts") or {}
    if not isinstance(facts, dict):
        facts = {}
    if not isinstance(mdb, dict):
        mdb = {}

    trusted_keywords = [
        *list(features.get("keywords") or []),
        *list(facts.get("keywords") or []),
    ]
    mdb_keywords = list(mdb.get("keywords") or [])
    trusted_keyword_text = " ".join(str(value) for value in trusted_keywords)
    mdb_keyword_text = " ".join(str(value) for value in mdb_keywords)
    overview = str(features.get("overview") or "")
    factual_text = " ".join((overview, trusted_keyword_text))

    raw_countries = list(features.get("countries") or [])
    raw_countries.extend(facts.get("countries") or [])
    if mdb.get("country"):
        raw_countries.append(mdb["country"])
    countries = {normalize(value) for value in raw_countries if value}
    language = normalize(features.get("original_language") or "")

    safe_mdb_facets = frozenset({"source_franchise", "style", "pedigree"})
    out: dict[str, float] = {}
    for tag in shortlist(features):
        if tag.derived or is_model_interpretive(tag):
            continue
        if tag.name in PROMPT_SUPPRESSED_NAMES:
            continue

        variants = {
            normalize(tag.name),
            *(normalize(alias) for alias in tag.aliases),
        }
        variants.discard("")
        # A canonical name or multiword alias in the synopsis is strong enough
        # narrative evidence. Single-token aliases such as "escape" are not:
        # that collision caused The Pit -> Prison Break in job 227.
        narrative_variants = {
            normalize(tag.name),
            *(variant for variant in variants if len(normalize(variant).split()) >= 2),
        }
        trusted_support = _contains_variant(trusted_keyword_text, variants)
        narrative_support = _contains_variant(overview, narrative_variants)
        mdb_support = _contains_variant(mdb_keyword_text, variants)

        supported = trusted_support or narrative_support
        if tag.facet in safe_mdb_facets:
            supported = supported or mdb_support
        if tag.facet == "place" and tag.name in MDB_NAMED_PLACE_NAMES:
            supported = supported or mdb_support

        if tag.name == "Anime":
            explicit_anime = _contains_variant(
                " ".join((trusted_keyword_text, mdb_keyword_text)),
                ("anime", "japanese animation"),
            )
            japanese_origin = language == "ja" or bool({"jp", "japan"} & countries)
            supported = explicit_anime and japanese_origin

        if tag.name == "Cinematic Universe":
            supported = _contains_variant(
                " ".join((factual_text, mdb_keyword_text)),
                (
                    "cinematic universe",
                    "marvel cinematic universe",
                    "dc extended universe",
                    "dc universe",
                ),
            )

        if not supported:
            continue
        out[tag.name] = 1.0

    # Manga is the precise source label; a provider keyword that also says
    # "based on comic" must not double-write the generic adaptation.
    if "Manga Adaptation" in out:
        out.pop("Comic Book Adaptation", None)
    return out


def _batch_key(features: dict[str, Any]) -> tuple[str, str, int]:
    """Cluster key: same media type, primary genre and decade batch together."""
    media_type = str(features.get("media_type") or "movie")
    genres = features.get("genres") or []
    primary = str(genres[0]) if genres else ""
    release = str(features.get("release_date") or "")
    decade = 0
    if len(release) >= 4 and release[:4].isdigit():
        decade = (int(release[:4]) // 10) * 10
    return (media_type, primary, decade)


def group_into_batches(
    items: Sequence[dict[str, Any]], size: int
) -> Iterator[list[dict[str, Any]]]:
    """Yield batches clustered so their shortlists overlap heavily.

    Clustering by (media_type, primary genre, decade) means the union of a
    batch's shortlists -- which is what actually goes in the prompt -- stays
    small, instead of being the sum of unrelated menus.
    """
    if size <= 0:
        raise ValueError("size must be positive")
    buckets: dict[tuple[str, str, int], list[dict[str, Any]]] = {}
    for item in items:
        buckets.setdefault(_batch_key(item), []).append(item)
    for key in sorted(buckets, key=lambda k: (k[0], k[1], -k[2])):
        bucket = buckets[key]
        for start in range(0, len(bucket), size):
            yield bucket[start : start + size]


def batch_vocabulary(batch: Sequence[dict[str, Any]]) -> list[TagDef]:
    """The deduplicated union of a batch's shortlists, in facet order."""
    union: dict[str, TagDef] = {}
    for item in batch:
        for tag in shortlist(item):
            union[tag.name] = tag
    return sorted(
        union.values(), key=lambda t: (FACETS.index(t.facet), t.sort_hint, t.name)
    )


def model_batch_vocabulary(batch: Sequence[dict[str, Any]]) -> list[TagDef]:
    """The interpretive-only vocabulary sent to a paid provider."""

    union: dict[str, TagDef] = {}
    for item in batch:
        for tag in model_shortlist(item):
            union[tag.name] = tag
    return sorted(
        union.values(), key=lambda t: (FACETS.index(t.facet), t.sort_hint, t.name)
    )


def prompt_shard(tags: Iterable[TagDef], *, indexed: bool = False) -> str:
    """Render the ALLOWED TAGS block for a prompt.

    One line per tag: ``[facet] Name -- gloss``. This is the only place the
    vocabulary is turned into prompt text, so the prompt can no longer drift
    from the database the way it had before this module existed.
    """
    lines = [
        (
            f"{index}: [{tag.facet}] {tag.name} — {tag.gloss}"
            if indexed
            else f"[{tag.facet}] {tag.name} — {tag.gloss}"
        )
        for index, tag in enumerate(tags)
    ]
    return "\n".join(lines)


def estimate_tokens(text: str) -> int:
    """Rough token count. Deliberately crude -- used only for budget assertions."""
    return len(text) // 4


# ---------------------------------------------------------------------------
# Row naming (consumed in Phase 5; defined here so names live with the tags)
# ---------------------------------------------------------------------------
#: Setting-period tags that read correctly with a definite article ("set in
#: **the** Cold War") versus those that do not ("set in **the** World War II").
#: An explicit set beats a heuristic here -- there are 23 values, they change
#: rarely, and getting it wrong is visible in a row title.
PERIOD_TAKES_THE: frozenset[str] = frozenset(
    {
        "Ancient World",
        "Renaissance",
        "Age of Sail",
        "Victorian Era",
        "Wild West Era",
        "Belle Epoque",
        "Great Depression",
        "Cold War",
        "1960s Setting",
        "1970s Setting",
        "1980s Setting",
        "1990s Setting",
        "Near Future",
        "Far Future",
        "Post-War Years",
    }
)

#: Last tokens that must never be pluralised -- roman numerals and digits.
_NO_PLURAL_TOKENS = frozenset(
    {"i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x"}
)

# High-visibility row names deserve real English.  These are deliberately
# explicit rather than attempting to turn the naming layer into an inflector.
_IRREGULAR_ROW_PLURALS: dict[str, str] = {
    "anti-hero": "Anti-Heroes",
    "hero": "Heroes",
    "rise and fall": "Rise-and-Fall Stories",
}


def pluralise(name: str) -> str:
    """Pluralise the final word of a tag name for use in a row title.

    Naive ``name + "s"`` produced *Documentarys*, *Mockumentarys* and
    *Comedys* in real generated rows -- visible, embarrassing, and in front of
    users. This handles the three cases English actually needs:

    * consonant + ``y`` -> ``ies``   (Documentary -> Documentaries)
    * sibilant endings  -> ``es``    (Box -> Boxes)
    * already plural    -> unchanged (Series stays Series)

    Roman numerals are left alone so "World War II" cannot become
    "World War IIs".
    """
    exact = _IRREGULAR_ROW_PLURALS.get(name.lower())
    if exact:
        return exact

    head, _, last = name.rpartition(" ")
    word = last or name
    lower = word.lower()

    irregular = _IRREGULAR_ROW_PLURALS.get(lower)
    if irregular:
        plural = irregular
    elif lower in _NO_PLURAL_TOKENS or word.isdigit():
        return name
    elif lower.endswith("s") and not lower.endswith("ss"):
        plural = word  # Series, Credits -- already plural
    elif lower.endswith(("s", "x", "z", "ch", "sh")):
        plural = word + "es"
    elif len(word) > 1 and lower.endswith("y") and lower[-2] not in "aeiou":
        plural = word[:-1] + "ies"
    else:
        plural = word + "s"
    return f"{head} {plural}" if head else plural


def period_phrase(period: str) -> str:
    """A setting-period tag rendered for "Set in ___"."""
    return f"the {period}" if period in PERIOD_TAKES_THE else period


#: Facet-pair -> row-name template. ``{a}``/``{b}`` are the tag names,
#: ``{as}``/``{bs}`` their plurals, ``{ap}`` a period with its article.
#:
#: Coverage matters more than elegance: an uncovered pair falls through to a
#: bare "A B" join, which produced row titles like "AI Ethics Near Future" and
#: "Underdog Triumphant". Every pair the library actually generates should have
#: an entry.
NAME_TEMPLATES: dict[tuple[str, str], str] = {
    ("mood", "subgenre"): "{a} {bs}",
    ("origin", "subgenre"): "{a} {bs}",
    ("setting_period", "subgenre"): "{bs} Set in {ap}",
    ("place", "subgenre"): "{bs} Set in {a}",
    ("place", "mood"): "{b} Stories from {a}",
    ("theme", "subgenre"): "{bs} About {a}",
    ("character", "subgenre"): "{bs} Led by {as}",
    ("style", "subgenre"): "{a} {bs}",
    ("plot_device", "subgenre"): "{bs} with {a}",
    ("mood", "theme"): "{a} Stories About {b}",
    ("pacing_format", "subgenre"): "{a} {bs}",
    ("release_decade", "subgenre"): "{a} {bs}",
    ("source_franchise", "subgenre"): "{bs} {a}",
    ("seasonal", "subgenre"): "{a} {bs}",
    ("pedigree", "subgenre"): "{a} {bs}",
    # --- pairs the first production preview exposed as uncovered ---
    ("audience", "subgenre"): "{bs} for {a}",
    ("audience", "mood"): "{b} Picks for {a}",
    ("theme", "setting_period"): "{a} in {bp}",
    ("theme", "character"): "{bs} and {a}",
    ("character", "mood"): "{a} Stories with {b} Energy",
    ("mood", "plot_device"): "{a} {bs}",
    ("mood", "pacing_format"): "{b} and {a}",
    ("theme", "place"): "{a} in {b}",
    ("theme", "plot_device"): "{bs} About {a}",
    ("style", "mood"): "{a}, {b}",
    ("origin", "mood"): "{a} {b} Picks",
    ("plot_device", "character"): "{bs} in {a}",
    ("pedigree", "mood"): "{a} {b} Picks",
    ("setting_period", "mood"): "{b} {ap}",
    ("setting_period", "theme"): "{b} in {ap}",
    ("release_decade", "mood"): "{b} {a}",
    # --- bare joins found in the first full-database preview ---
    ("setting_period", "place"): "{b} Stories from {ap}",
    ("place", "audience"): "{b} Stories Set in {a}",
}

#: Facet -> template for a personalized taste row, replacing the old
#: "{tag} For You" that produced rows called "Dark For You".
TASTE_ROW_TEMPLATES: dict[str, str] = {
    "mood": "Because You Like It {name}",
    "subgenre": "More {name}",
    "theme": "Stories About {name}",
    "character": "Led by {name}s",
    "plot_device": "More {name} Stories",
    "style": "{name} Picks",
    "place": "Set in {name}",
    "setting_period": "Set in the {name}",
    "origin": "{name} Picks",
    "pacing_format": "{name} Watches",
    "audience": "{name}",
    "source_franchise": "{name}",
    "seasonal": "{name} Picks",
    "pedigree": "{name}",
    "release_decade": "The {name}",
    # maturity tags are display=False and should never name a row, but the map
    # is kept total so the lookup can never fall through silently.
    "maturity": "{name}",
}


def taste_row_name(tag: TagDef) -> str:
    """Name a personalized taste row for a tag."""
    template = TASTE_ROW_TEMPLATES.get(tag.facet, "{name}")
    return template.format(name=tag.name)


def facet_counts() -> dict[str, int]:
    """Tag count per facet -- used by tests and the admin QA dashboard."""
    return {facet: len(BY_FACET[facet]) for facet in FACETS}
