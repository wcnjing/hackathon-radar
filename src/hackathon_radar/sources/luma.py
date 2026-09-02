"""Luma (lu.ma) city discover pages — networking events, meetups, demo days.

The city page embeds a __NEXT_DATA__ JSON blob with ~20 events and the city's
place id; the discover API then returns the full upcoming list for that place.
"""

import json
import logging
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx

from hackathon_radar.models import Event

log = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
PAGE_URL = "https://lu.ma/{city}"
API_URL = "https://api.lu.ma/discover/get-paginated-events"


def extract_next_data(html: str) -> dict:
    match = re.search(r'id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not match:
        raise ValueError("no __NEXT_DATA__ blob in Luma page")
    return json.loads(match.group(1))


HACKATHON_TITLE_RE = re.compile(r"hack|buildathon|build-a-thon|sprint|\bjam\b", re.I)
# Luma registration_availability values that mean "you can't just sign up".
FULL_AVAILABILITY = {"waitlist", "sold_out", "closed"}


def parse_entry(entry: dict) -> Event | None:
    e = entry.get("event") or {}
    if not e.get("api_id") or not e.get("name") or not e.get("url"):
        return None
    geo = e.get("geo_address_info") or {}
    hosts = [h["name"] for h in entry.get("hosts") or [] if h.get("name")]
    title = e["name"].strip()
    return Event(
        source="luma",
        external_id=e["api_id"],
        title=title,
        # Heuristic default; Claude scoring refines the kind when available.
        kind="hackathon" if HACKATHON_TITLE_RE.search(title) else "networking",
        url=f"https://lu.ma/{e['url']}",
        dates_text=_format_start(e.get("start_at"), e.get("timezone")),
        starts_at=e.get("start_at"),
        ends_at=e.get("end_at"),
        location=geo.get("address") or geo.get("city_state") or "",
        country=geo.get("country_code"),
        online=e.get("location_type") == "online",
        full=entry.get("registration_availability") in FULL_AVAILABILITY,
        organizer=", ".join(hosts[:3]) or None,
    )


def _format_start(starts_at: str | None, tz: str | None) -> str:
    if not starts_at:
        return ""
    try:
        dt = datetime.fromisoformat(starts_at.replace("Z", "+00:00"))
        if tz:
            dt = dt.astimezone(ZoneInfo(tz))
        day = str(dt.day)
        hour12 = str(int(dt.strftime("%I")))
        return f"{dt:%a %b} {day}, {hour12}:{dt:%M %p}"
    except Exception:
        return ""


def _upcoming(event: Event, now: datetime) -> bool:
    stamp = event.ends_at or event.starts_at
    if not stamp:
        return True
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")) >= now
    except ValueError:
        return True


def fetch(source_cfg: dict) -> list[Event]:
    city = source_cfg.get("city", "singapore")
    limit = source_cfg.get("limit", 50)
    now = datetime.now(timezone.utc)

    with httpx.Client(
        headers={"User-Agent": USER_AGENT}, timeout=30, follow_redirects=True
    ) as client:
        page = client.get(PAGE_URL.format(city=city))
        page.raise_for_status()
        data = extract_next_data(page.text)["props"]["pageProps"]["initialData"]["data"]
        entries = data.get("events", [])

        # The embedded blob is capped (~20); the discover API returns the full list.
        place_id = (data.get("place") or {}).get("api_id")
        if place_id:
            try:
                resp = client.get(
                    API_URL,
                    params={"discover_place_api_id": place_id, "pagination_limit": limit},
                )
                resp.raise_for_status()
                api_entries = resp.json().get("entries", [])
                if api_entries:
                    entries = api_entries
            except Exception as exc:
                log.warning("luma discover API failed (%s); using embedded events", exc)

    events: dict[str, Event] = {}
    for entry in entries:
        event = parse_entry(entry)
        if event and _upcoming(event, now):
            events[event.external_id] = event
    return list(events.values())
