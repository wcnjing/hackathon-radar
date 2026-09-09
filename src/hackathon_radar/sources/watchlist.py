"""Watchlist — arbitrary organizer pages (company hackathon sites, school clubs).

These pages share no common format, so Claude extracts events from the page
text. A content hash per page means extraction only runs when a page actually
changes; unchanged pages cost nothing.
"""

import hashlib
import json
import logging
from datetime import date
from urllib.parse import urljoin

import httpx
from pydantic import BaseModel

from hackathon_radar.config import PROJECT_ROOT
from hackathon_radar.enrich import _page_text
from hackathon_radar.filtering import is_usable_url, normalize_title
from hackathon_radar.models import Event

log = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
STATE_PATH = PROJECT_ROOT / "data" / "watchlist_state.json"

PROMPT = """Today is {today}. Below is the text of {page_url}, a page where a tech
event organizer lists their events.

Extract every tech event (hackathon, workshop, meetup, talk, program) that is
upcoming or currently open for registration. Skip past events, and skip anything
that is clearly navigation, a sponsor mention, or not an actual event. If the page
lists no upcoming events, return an empty list.

For links, prefer the most informative public event page where a newcomer can
read details and decide. Avoid direct signup/login walls, Google Forms, "apply
now" pages, calendar-add links, and tracking redirects when a public detail page
is available.

Organizer sites often go stale: when an event's date has no year, assume the
current year — if that makes it past, skip it even if the page says "upcoming".

For each event: title; url (best public detail link, relative is fine, null if none);
dates_text (dates/time as written, null if none); location (null if not stated);
country_code (two-letter code when the location or page context makes it clear,
e.g. a university room on an SG campus -> "SG", New York -> "US"; null if truly
unclear); is_online (true for virtual/remote events)."""


class PageEvent(BaseModel):
    title: str
    url: str | None
    dates_text: str | None
    location: str | None
    country_code: str | None
    is_online: bool


class PageEvents(BaseModel):
    events: list[PageEvent]


def _load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except OSError, ValueError:
        return {}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=1))


def _resolve_url(raw: str | None, page_url: str) -> str:
    """The event's own link when it resolves to something usable, else the page.

    `urljoin` is not a validator. It passes an absolute non-http(s) scheme
    straight through (`mailto:a@b` stays `mailto:a@b`), and it buries a
    schemeless domain under the page's own path (`www.foo.org/e` becomes
    `<page>/www.foo.org/e`, a silent 404 that still looks like a valid URL).

    Falling back rather than skipping: `page_url` comes from trusted config and
    is a real, informative link, so an unusable event link degrades to it — the
    same thing that already happens when a page gives no per-event link at all.
    """
    if not raw:
        return page_url
    # A relative path starting with "www." is a domain whose scheme the
    # extractor dropped. Caught before urljoin, which would otherwise hide it
    # behind a URL that passes every later check. Deliberately narrow: real
    # relative paths do start with dots ("index.html", "assets/v1.2/x"), so a
    # broader domain-shaped test would reject working links.
    if raw.startswith("www."):
        log.info("watchlist event link %r is a schemeless domain; using the page", raw)
        return page_url
    joined = urljoin(page_url, raw)
    if not is_usable_url(joined):
        log.info("watchlist event link %r is not openable; using the page", raw)
        return page_url
    return joined


def to_event(pe: PageEvent, page_url: str, assume_country: str) -> Event | None:
    title = pe.title.strip()
    if not title:
        return None
    url = _resolve_url(pe.url, page_url)
    return Event(
        source="watchlist",
        # The page may not give events stable links, so key on page + title.
        external_id=f"{page_url}#{normalize_title(title)}",
        title=title,
        url=url,
        dates_text=pe.dates_text or "",
        location=pe.location or "",
        online=pe.is_online,
        # Trust the per-event country when the page states one (global pages
        # like Jane Street list events worldwide); fall back to the configured
        # country so local organizers' events aren't dropped by the scope filter.
        country=pe.country_code or assume_country or None,
    )


def _extract(client, model: str, text: str, page_url: str) -> list[PageEvent]:
    response = client.messages.parse(
        model=model,
        max_tokens=2_000,
        messages=[
            {
                "role": "user",
                "content": PROMPT.format(today=date.today().isoformat(), page_url=page_url)
                + f"\n\n<page_text>\n{text}\n</page_text>",
            }
        ],
        output_format=PageEvents,
    )
    return response.parsed_output.events


def fetch(source_cfg: dict) -> list[Event]:
    pages = source_cfg.get("pages", [])
    if not pages:
        return []

    from hackathon_radar.scoring import make_client

    try:
        client = make_client()
    except Exception as exc:
        log.info("watchlist needs Anthropic credentials (%s); skipping", exc)
        return []

    model = source_cfg.get("model", "claude-haiku-4-5")
    assume_country = source_cfg.get("assume_country", "SG")
    state = _load_state()
    events: list[Event] = []

    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=30, follow_redirects=True) as web:
        for page_url in pages:
            try:
                text = _page_text(web.get(page_url).text, 8_000)
            except httpx.HTTPError as exc:
                log.warning("watchlist fetch failed for %s: %s", page_url, exc)
                continue

            digest = hashlib.sha256(text.encode()).hexdigest()
            if state.get(page_url) == digest:
                continue  # unchanged since last successful extraction — free skip

            try:
                page_events = _extract(client, model, text, page_url)
            except Exception as exc:
                log.warning("watchlist extraction failed for %s: %s", page_url, exc)
                continue  # hash not saved, so it retries next run

            for pe in page_events:
                event = to_event(pe, page_url, assume_country)
                if event:
                    events.append(event)
            state[page_url] = digest
            log.info("watchlist: %s changed, %d event(s) extracted", page_url, len(page_events))

    _save_state(state)
    return events
