import json
from datetime import date
from pathlib import Path

import pytest

from hackathon_radar.sources import devpost, luma, mlh

FIXTURES = Path(__file__).parent / "fixtures"


def test_devpost_parse():
    data = json.loads((FIXTURES / "devpost.json").read_text(encoding="utf-8"))
    events = devpost.parse_response(data)
    assert events, "fixture should yield events"

    first = events[0]
    assert first.source == "devpost"
    assert first.title == "Build with Gemini XPRIZE"
    assert first.url.startswith("https://")
    assert first.online is True
    assert "Machine Learning/AI" in first.tags
    assert first.prize == "$2,000,000"  # HTML span stripped
    # labelled so a past start date doesn't read as "already over"
    assert first.dates_text.startswith("submissions ")
    assert first.time_left  # e.g. "about 1 month left"
    assert first.register_url is None  # card links to the public overview, not the signup wall


def test_devpost_skips_closed_and_flags_invite_only():
    data = {
        "hackathons": [
            {"id": 1, "title": "Closed", "open_state": "ended", "themes": []},
            {"id": 2, "title": "Invite", "open_state": "open", "invite_only": True, "themes": []},
            {
                "id": 3,
                "title": "Open",
                "open_state": "open",
                "themes": [],
                "url": "https://x.devpost.com",
            },
        ]
    }
    events = devpost.parse_response(data)
    assert [e.title for e in events] == ["Invite", "Open"]
    assert [e.invite_only for e in events] == [True, False]


def test_mlh_parse():
    html = (FIXTURES / "mlh2027.html").read_text(encoding="utf-8")
    events = mlh.parse_season_page(html)
    assert len(events) > 30, "season page should have many events"

    first = events[0]
    assert first.source == "mlh"
    assert first.title
    assert first.url.startswith("https://")
    assert first.starts_at and "T" in first.starts_at  # ISO from microdata
    assert first.location
    assert "student hackathon" in first.tags

    # microdata distinguishes online vs in-person
    assert any(e.online for e in events) or any(not e.online for e in events)
    # country codes come through for in-person events
    assert any(e.country for e in events if not e.online)


def test_luma_parse():
    data = json.loads((FIXTURES / "luma_api.json").read_text(encoding="utf-8"))
    events = [e for e in (luma.parse_entry(entry) for entry in data["entries"]) if e]
    assert len(events) > 30, "SG discover feed should have many events"

    first = events[0]
    assert first.source == "luma"
    assert first.title == "AI For Normies - Copilot"
    assert first.url.startswith("https://lu.ma/")
    assert first.country == "SG"
    assert first.online is False
    assert first.starts_at and "T" in first.starts_at
    assert first.dates_text == "Tue Jul 7, 6:30 PM"

    assert any(e.organizer for e in events), "host names should come through"


def test_luma_parse_entry_skips_malformed():
    assert luma.parse_entry({}) is None
    assert luma.parse_entry({"event": {"api_id": "x", "name": "No URL"}}) is None


def test_luma_flags_full_events():
    data = json.loads((FIXTURES / "luma_api.json").read_text(encoding="utf-8"))
    by_id = {}
    for entry in data["entries"]:
        ev = luma.parse_entry(entry)
        if ev:
            by_id[ev.external_id] = (ev, entry.get("registration_availability"))
    # every waitlist event is flagged full; every open one is not
    assert any(full for full, _ in ((ev.full, a) for ev, a in by_id.values()))
    for ev, availability in by_id.values():
        assert ev.full == (availability in luma.FULL_AVAILABILITY)


def test_luma_kind_heuristic():
    def entry(name):
        return {"event": {"api_id": "x", "name": name, "url": "slug"}}

    assert luma.parse_entry(entry("Daytona HackSprint")).kind == "hackathon"
    assert luma.parse_entry(entry("AI Buildathon Night")).kind == "hackathon"
    assert luma.parse_entry(entry("Founders' Breakfast")).kind == "networking"
    # "jam" needs word boundaries â€” "Jamie's Talk" is not a game jam
    assert luma.parse_entry(entry("Jamie's Fireside Chat")).kind == "networking"


def test_mlh_upcoming_filter():
    from hackathon_radar.models import Event

    past = Event(source="mlh", external_id="a", title="a", url="u", ends_at="2020-01-01T00:00:00Z")
    future = Event(
        source="mlh", external_id="b", title="b", url="u", ends_at="2099-01-01T00:00:00Z"
    )
    today = date(2026, 7, 7)
    assert not mlh._upcoming(past, today)
    assert mlh._upcoming(future, today)


@pytest.mark.parametrize(
    ("stamp", "tz", "expected"),
    [
        ("2026-07-07T00:00:00Z", None, "Tue Jul 7, 12:00 AM"),
        ("2026-07-07T12:00:00Z", "UTC", "Tue Jul 7, 12:00 PM"),
        ("2026-07-07T01:05:00Z", None, "Tue Jul 7, 1:05 AM"),
        ("2026-07-06T16:00:00Z", "Asia/Singapore", "Tue Jul 7, 12:00 AM"),
        ("2026-07-07T00:00:00Z", "Australia/Eucla", "Tue Jul 7, 8:45 AM"),
    ],
)
def test_luma_format_start(stamp, tz, expected):
    assert luma._format_start(stamp, tz) == expected


def test_mlh_date_format():
    html = (FIXTURES / "mlh2027.html").read_text(encoding="utf-8")
    events = mlh.parse_season_page(html)
    assert all(event.dates_text for event in events)
    assert events[0].dates_text == "Aug 29 - Aug 30, 2026"
