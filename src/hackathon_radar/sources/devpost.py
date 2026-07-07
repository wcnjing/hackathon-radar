"""Devpost — undocumented but stable JSON API used by devpost.com/hackathons."""

import re

import httpx

from hackathon_radar.models import Event

API_URL = "https://devpost.com/api/hackathons"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

_TAG_RE = re.compile(r"<[^>]+>")


def parse_hackathon(h: dict) -> Event:
    location = (h.get("displayed_location") or {}).get("location", "") or ""
    prize_raw = h.get("prize_amount") or ""
    prize = _TAG_RE.sub("", prize_raw).strip() or None
    return Event(
        source="devpost",
        external_id=str(h["id"]),
        title=h.get("title", "").strip(),
        url=h.get("url", ""),
        dates_text=h.get("submission_period_dates", ""),
        location=location,
        online=location.lower() == "online",
        tags=[t["name"] for t in h.get("themes", [])],
        prize=prize,
    )


def parse_response(data: dict) -> list[Event]:
    events = []
    for h in data.get("hackathons", []):
        if h.get("open_state") not in ("open", "upcoming"):
            continue
        if h.get("invite_only"):
            continue
        events.append(parse_hackathon(h))
    return events


def fetch(source_cfg: dict) -> list[Event]:
    pages = source_cfg.get("pages", 2)
    events: list[Event] = []
    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=30) as client:
        for page in range(1, pages + 1):
            resp = client.get(
                API_URL,
                params={"page": page, "status[]": ["upcoming", "open"]},
            )
            resp.raise_for_status()
            events.extend(parse_response(resp.json()))
    return events
