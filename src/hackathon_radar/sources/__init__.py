import logging

from hackathon_radar.models import Event
from hackathon_radar.sources import devpost, email_source, luma, mlh, watchlist

log = logging.getLogger(__name__)

FETCHERS = {
    "devpost": devpost.fetch,
    "mlh": mlh.fetch,
    "luma": luma.fetch,
    "watchlist": watchlist.fetch,
    "email": email_source.fetch,
}


def fetch_all(config: dict) -> list[Event]:
    """Fetch from every enabled source; one source failing doesn't kill the run."""
    events: list[Event] = []
    for name, fetch in FETCHERS.items():
        source_cfg = config.get("sources", {}).get(name, {})
        if not source_cfg.get("enabled", True):
            continue
        try:
            fetched = fetch(source_cfg)
            log.info("%s: %d events", name, len(fetched))
            events.extend(fetched)
        except Exception:
            log.exception("%s: fetch failed, skipping", name)
    return events
