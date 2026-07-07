"""MLH (Major League Hacking) — season event pages carry schema.org microdata."""

from datetime import date, datetime, timezone

import httpx
from bs4 import BeautifulSoup

from hackathon_radar.models import Event

SEASON_URL = "https://mlh.io/seasons/{year}/events"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"


def _meta(card, prop: str) -> str | None:
    tag = card.find("meta", attrs={"itemprop": prop})
    return tag.get("content") if tag else None


def parse_season_page(html: str) -> list[Event]:
    soup = BeautifulSoup(html, "html.parser")
    events = []
    for card in soup.select('a[itemtype="https://schema.org/Event"]'):
        url = _meta(card, "url") or card.get("href", "")
        title_tag = card.find("h4")
        title = title_tag.get_text(strip=True) if title_tag else ""
        if not url or not title:
            continue

        mode = _meta(card, "eventAttendanceMode") or ""
        location_div = card.find(attrs={"itemprop": "location"})
        location = ""
        country = None
        if location_div:
            name = location_div.find(attrs={"itemprop": "name"})
            location = name.get_text(" ", strip=True) if name else ""
            country_meta = location_div.find("meta", attrs={"itemprop": "addressCountry"})
            country = country_meta.get("content") if country_meta else None

        starts_at = _meta(card, "startDate")
        ends_at = _meta(card, "endDate")
        dates_text = ""
        if starts_at and ends_at:
            try:
                s = datetime.fromisoformat(starts_at.replace("Z", "+00:00"))
                e = datetime.fromisoformat(ends_at.replace("Z", "+00:00"))
                dates_text = f"{s:%b %-d} - {e:%b %-d, %Y}"
            except ValueError:
                pass

        events.append(
            Event(
                source="mlh",
                external_id=url.rstrip("/"),
                title=title,
                url=url,
                dates_text=dates_text,
                starts_at=starts_at,
                ends_at=ends_at,
                location=location or ("Online" if "Online" in mode else ""),
                country=country,
                online="Online" in mode,
                tags=["student hackathon"],
            )
        )
    return events


def _upcoming(event: Event, today: date) -> bool:
    if not event.ends_at:
        return True
    try:
        end = datetime.fromisoformat(event.ends_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    return end.date() >= today


def fetch(source_cfg: dict) -> list[Event]:
    today = datetime.now(timezone.utc).date()
    # MLH seasons are named for the year they end in; fetch this year's and next.
    years = (today.year, today.year + 1)
    seen: dict[str, Event] = {}
    with httpx.Client(
        headers={"User-Agent": USER_AGENT}, timeout=30, follow_redirects=True
    ) as client:
        for year in years:
            resp = client.get(SEASON_URL.format(year=year))
            if resp.status_code == 404:
                continue
            resp.raise_for_status()
            for event in parse_season_page(resp.text):
                if _upcoming(event, today):
                    seen[event.external_id] = event
    return list(seen.values())
