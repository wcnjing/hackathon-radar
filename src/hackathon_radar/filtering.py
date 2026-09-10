"""Scope filtering and the keyword fallback scorer."""

import re
from typing import Literal

from hackathon_radar.models import Event

# One definition of the taxonomy, imported by `scoring.ScoredEvent` too, so the
# fallback classifier and the model's structured output cannot drift apart.
Kind = Literal["hackathon", "networking", "program"]

# Sources that publish build-and-compete events exclusively. Checked before any
# regex because the source is a stronger signal than the title: "MHacks 2026"
# and "TartanHacks" carry no token a regex can anchor on without also matching
# "shack", and both are unambiguously hackathons by virtue of where they came
# from.
HACKATHON_ONLY_SOURCES = ("devpost", "mlh")

NETWORKING_TITLE_RE = re.compile(
    r"\b(meetup|mixer|social|demo day|founders?['’]? (breakfast|coffee)|"
    r"talk|panel|fireside|networking|conference|summit|expo)\b",
    re.I,
)
PROGRAM_TITLE_RE = re.compile(
    r"\b(accelerator|fellowship|cohort|bootcamp|incubat\w*|apprenticeship|residency)\b", re.I
)
# Build-event signals come in two strengths, and the difference decides who
# wins against a networking word in the same title.
#
# STRONG words name the format outright. Nothing else is called a hackathon or a
# datathon, so these beat any networking word: "Hackathon Demo Day" is a
# hackathon that happens to end in a demo, not a demo day.
#
# `hacka` catches hackathon/hackathons; `hacks\b` catches MHacks and TartanHacks
# where the prefix is glued on; `\bhack\b` catches "Hack&Roll". Deliberately not
# a bare `hack`, which would also match "shack" and "hackney".
STRONG_HACKATHON_RE = re.compile(
    r"hacka|hacks\b|\bhack\b|\bbuildathon\b|\bbuild-a-thon\b|\bdatathon\b", re.I
)

# WEAK words merely suggest building. Every one of them appears happily in the
# name of a mixer -- "Build Club Mixer", "Pitch Competition & Networking Night",
# "Demo Day Jam Session" -- so they lose to an explicit networking word and are
# only consulted once none is present. Ranking them above networking let six of
# seven such titles clear the base bar of 6 and post, which is precisely what
# issue #4's networking bar exists to prevent.
#
# Each is `\b`-anchored on both sides. An unanchored `sprint` once classified
# "Sprinter Van Expo" as a hackathon; `\bbuild\b`'s closing boundary is what
# keeps "AI Builders Mixer" out while letting "Build with AI" through.
WEAK_BUILD_RE = re.compile(
    r"\bsprint\b|\bjam\b|\bworkshop\b|\bleague\b|\bbuild\b|"
    r"\b(challenge|competition|contest)\b",
    re.I,
)


def classify_kind(event: Event) -> Kind:
    """The kind alone. See `classify_kind_with_signal` for the reasoning."""
    return classify_kind_with_signal(event)[0]


def classify_kind_with_signal(event: Event) -> tuple[Kind, bool]:
    """Best-effort kind classification, plus whether anything actually matched.

    The second element is False when no rule fired and the default was used.
    Callers need that distinction: "this is a hackathon because it says
    buildathon" and "this might be anything, so assume the stricter bar" are
    very different claims, and only the first is evidence about the event.

    Claude overrides all of this when available; it only runs when Claude isn't.

    Precedence, strongest claim first:

        1. source        devpost/mlh publish nothing else
        2. strong build  "hackathon", "datathon" — names the format outright
        3. program       "accelerator", "fellowship"
        4. networking    "mixer", "meetup", "networking"
        5. weak build    "build", "jam", "league", "challenge"
        6. default       networking, with no signal reported

    The split at 2/5 is the whole point. Ranking *every* build word above
    networking fixes "Social Impact Hackathon" and "Hackathon Demo Day", which
    were filed as networking on the strength of "social" and "demo day" — but it
    also hands the base bar of 6 to "Build Club Mixer", "Pitch Competition &
    Networking Night" and "Founders Breakfast: Build in Public", all of which
    then post. Strong words earn that precedence; weak ones do not, because a
    mixer will happily call itself a jam.

    Searches tags as well as the title: MLH stamps every event
    `tags=["student hackathon"]` and Devpost carries its themes, so the tags are
    often a cleaner signal than a marketing title. Location is deliberately left
    out — a venue called "The Hive Social" is not a networking event.
    """
    if event.source in HACKATHON_ONLY_SOURCES:
        return "hackathon", True
    haystack = " ".join([event.title, *event.tags]).lower()
    if STRONG_HACKATHON_RE.search(haystack):
        return "hackathon", True
    if PROGRAM_TITLE_RE.search(haystack):
        return "program", True
    if NETWORKING_TITLE_RE.search(haystack):
        return "networking", True
    if WEAK_BUILD_RE.search(haystack):
        return "hackathon", True
    # No signal either way. Defaulting to networking is the cautious read: it
    # applies the higher bar to something we cannot vouch for. That is only
    # tolerable because a degraded-path score is no longer final — `cli._select`
    # leaves such events unrecorded so the next run re-scores them with Claude
    # (see `scoring.ScoreResult.degraded`). Without that, this default would
    # permanently bury every event it guessed wrong about.
    return "networking", False


def in_scope(event: Event, scope_cfg: dict) -> bool:
    mode = scope_cfg.get("mode", "sg_plus_online")
    if mode == "global":
        return True

    home_country = scope_cfg.get("home_country", "SG")
    home_city = scope_cfg.get("home_city", "singapore").lower()
    local = (event.country == home_country) or (home_city in event.location.lower())

    if mode == "sg_only":
        return local and not event.online
    # sg_plus_online: anything local, plus anything joinable remotely
    return local or event.online


def normalize_title(title: str) -> str:
    """Collapse a title for duplicate detection across sources and reposts."""
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


# Reasons carrying this prefix are debug detail for the database; the Telegram
# message suppresses them (only Claude's "why you'd care" lines are shown).
KEYWORD_REASON_PREFIX = "keywords: "


def keyword_score(event: Event, interests_cfg: dict) -> tuple[float, str]:
    """Fallback scorer when Claude isn't available. Coarse but predictable."""
    keywords = [k.lower() for k in interests_cfg.get("keywords", [])]
    haystack = " ".join([event.title, event.location, *event.tags]).lower()
    # Word-boundary match: "ai" must not hit inside "trainocate" or "sustainability".
    hits = sorted({k for k in keywords if re.search(rf"\b{re.escape(k)}\b", haystack)})
    score = min(10.0, 5.0 + 1.5 * len(hits))
    return score, KEYWORD_REASON_PREFIX + (", ".join(hits) if hits else "none matched")
