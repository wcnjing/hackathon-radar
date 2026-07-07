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
    url = h.get("url", "")
    # Every Devpost hackathon subdomain serves its signup page at /register
    register_url = url.rstrip("/") + "/register" if url else None
    # The date range is the submission window — label it so a past start date
    # doesn't read as "event already over" (you can join until submissions close).
    period = h.get("submission_period_dates", "")
    dates_text = f"submissions {period}" if period else ""
    return Event(
        source="devpost",
        external_id=str(h["id"]),
        title=h.get("title", "").strip(),
        url=url,
        dates_text=dates_text,
        time_left=h.get("time_left_to_submission") or None,
        location=location,
        online=location.lower() == "online",
        tags=[t["name"] for t in h.get("themes", [])],
        prize=prize,
        register_url=register_url,
        invite_only=bool(h.get("invite_only")),
        organizer=h.get("organization_name") or None,
    )


def parse_response(data: dict) -> list[Event]:
    events = []
    for h in data.get("hackathons", []):
        if h.get("open_state") not in ("open", "upcoming"):
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
