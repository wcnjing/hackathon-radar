import logging
from contextlib import contextmanager
from pathlib import Path

from hackathon_radar.models import Event
from hackathon_radar.sources import devpost, email_source, luma, mlh, watchlist

log = logging.getLogger(__name__)

SOURCE_MODULES = {
    "devpost": devpost,
    "mlh": mlh,
    "luma": luma,
    "watchlist": watchlist,
    "email": email_source,
}


@contextmanager
def preserve_source_state():
    """Restore every source-owned state file after a preview.

    Stateful source modules declare ``STATE_PATH``. Discovering those paths
    from the source registry keeps dry-run handling automatic for future
    sources that follow the same convention.
    """
    snapshots: dict[Path, bytes | None] = {}
    for source in SOURCE_MODULES.values():
        state_path = getattr(source, "STATE_PATH", None)
        if state_path is None:
            continue
        path = Path(state_path)
        try:
            snapshots[path] = path.read_bytes()
        except FileNotFoundError:
            snapshots[path] = None

    try:
        yield
    finally:
        for path, contents in snapshots.items():
            if contents is None:
                path.unlink(missing_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(contents)


def fetch_all(config: dict) -> list[Event]:
    """Fetch from every enabled source; one source failing doesn't kill the run."""
    events: list[Event] = []
    for name, source in SOURCE_MODULES.items():
        source_cfg = config.get("sources", {}).get(name, {})
        if not source_cfg.get("enabled", True):
            continue
        try:
            fetched = source.fetch(source_cfg)
            log.info("%s: %d events", name, len(fetched))
            events.extend(fetched)
        except Exception:
            log.exception("%s: fetch failed, skipping", name)
    return events
