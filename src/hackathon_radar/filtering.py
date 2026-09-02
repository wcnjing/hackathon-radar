"""Scope filtering and the keyword fallback scorer."""

import re

from hackathon_radar.models import Event


NETWORKING_TITLE_RE = re.compile(
    r"\b(meetup|mixer|social|demo day|founders?['’]? (breakfast|coffee)|"
    r"talk|panel|fireside|networking)\b", re.I
)
PROGRAM_TITLE_RE = re.compile(
    r"\b(accelerator|fellowship|cohort|bootcamp|incubat\w*)\b", re.I
)
HACKATHON_TITLE_RE = re.compile(
    r"\bhack|buildathon|build-a-thon|datathon|sprint|\bjam\b|"
    r"\b(challenge|competition|contest)\b", re.I
)


def classify_kind(event: Event) -> str:
    """Best-effort kind classification for the keyword-fallback path.
    Claude overrides this when available; this only runs when it isn't."""
    title = event.title.lower()
    if NETWORKING_TITLE_RE.search(title):
        return "networking"
    if PROGRAM_TITLE_RE.search(title):
        return "program"
    if HACKATHON_TITLE_RE.search(title):
        return "hackathon"
    # Ambiguous title with no keyword signal: treat as networking, not
    # hackathon — the safer default given networking's higher bar exists
    # specifically to keep low-effort events out.
    return "networking"


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
