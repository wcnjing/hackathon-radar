"""Scope filtering and the keyword fallback scorer."""

import re

from hackathon_radar.models import Event


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
